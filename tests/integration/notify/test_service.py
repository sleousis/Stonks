"""Scoped notification services: devices, preferences, quiet hours, the
user's webhook and the feed. A user only ever sees or changes their own."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stonks.accounts import AccountsError, NotFound, Scope, UserRepository
from stonks.notify import service
from stonks.notify.events import Audience, Event
from stonks.notify.prefs import Preference
from stonks.notify.router import NotificationRouter
from stonks.notify.settings import OutboxSettings

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc-DEVICE-TOKEN"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _keys() -> tuple[str, str]:
    pub = ec.generate_private_key(ec.SECP256R1()).public_key()
    return _b64(pub.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)), _b64(b"a" * 16)


def _register(state, scope, endpoint=ENDPOINT, **kw):
    p256dh, auth = _keys()
    return service.register_push_subscription(
        state, scope, endpoint=endpoint, p256dh=p256dh, auth=auth, **kw
    )


# ---- devices ---------------------------------------------------------------------


def test_register_list_remove(state, alice_scope):
    dev = _register(state, alice_scope, user_agent="Chrome on Android")
    assert dev.id.startswith("psh_")
    assert dev.endpoint_host == "fcm.googleapis.com"
    assert "DEVICE-TOKEN" not in repr(dev)
    [listed] = service.list_push_subscriptions(state, alice_scope)
    assert listed.id == dev.id and listed.user_agent == "Chrome on Android"
    service.remove_push_subscription(state, alice_scope, dev.id)
    assert service.list_push_subscriptions(state, alice_scope) == []
    [row] = state.sql("SELECT revoked_reason FROM push_subscriptions")
    assert row["revoked_reason"] == "user"


def test_register_is_audited_without_the_endpoint(state, alice_scope):
    _register(state, alice_scope)
    rows = state.sql("SELECT * FROM audit_log WHERE action = 'notify.push.register'")
    assert len(rows) == 1
    assert rows[0]["actor"] == alice_scope.actor
    assert "DEVICE-TOKEN" not in rows[0]["details_json"]


def test_re_registering_a_device_refreshes_it(state, alice_scope):
    first = _register(state, alice_scope)
    service.remove_push_subscription(state, alice_scope, first.id)
    again = _register(state, alice_scope)
    assert again.id == first.id
    assert len(service.list_push_subscriptions(state, alice_scope)) == 1


def test_a_device_moves_to_whoever_registers_it_last(state, alice_scope, bob_scope):
    # One browser, one push subscription: signing in as Bob moves it.
    _register(state, alice_scope)
    _register(state, bob_scope)
    assert service.list_push_subscriptions(state, alice_scope) == []
    assert len(service.list_push_subscriptions(state, bob_scope)) == 1


def test_cannot_remove_someone_elses_device(state, alice_scope, bob_scope):
    dev = _register(state, alice_scope)
    with pytest.raises(NotFound):
        service.remove_push_subscription(state, bob_scope, dev.id)
    with pytest.raises(NotFound):
        service.remove_push_subscription(state, alice_scope, "psh_missing")
    assert len(service.list_push_subscriptions(state, alice_scope)) == 1


def test_device_limit_retires_the_oldest(state, alice_scope):
    settings = OutboxSettings(max_devices_per_user=2)
    ids = [
        _register(state, alice_scope, endpoint=f"{ENDPOINT}{i}", settings=settings).id
        for i in range(3)
    ]
    listed = [d.id for d in service.list_push_subscriptions(state, alice_scope)]
    assert listed == ids[1:]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",  # not https
        "https://127.0.0.1/push",  # private
        "https://localhost/push",
        "https://169.254.169.254/latest/meta-data",
        "https://evil.example.com/push",  # not a push service
        "https://fcm.googleapis.com.evil.example/x",
        "https://fcm.googleapis.com/" + "x" * 3000,
    ],
)
def test_endpoint_must_be_a_known_push_service(state, alice_scope, endpoint):
    with pytest.raises(AccountsError):
        _register(state, alice_scope, endpoint=endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://web.push.apple.com/QGx",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
    ],
)
def test_known_push_services_are_accepted(state, alice_scope, endpoint):
    assert _register(state, alice_scope, endpoint=endpoint).id


def test_bad_keys_are_refused(state, alice_scope):
    with pytest.raises(AccountsError):
        service.register_push_subscription(
            state, alice_scope, endpoint=ENDPOINT, p256dh="short", auth=_b64(b"a" * 16)
        )
    p256dh, _ = _keys()
    with pytest.raises(AccountsError):
        service.register_push_subscription(
            state, alice_scope, endpoint=ENDPOINT, p256dh=p256dh, auth=_b64(b"a" * 5)
        )


def test_service_scopes_have_no_devices(state):
    with pytest.raises(AccountsError):
        _register(state, Scope.service("scheduler"))


def test_disabled_users_are_not_found(state, users, alice_scope):
    UserRepository(state).set_status(users["alice"].id, "disabled", actor="service:system")
    with pytest.raises(NotFound):
        _register(state, alice_scope)
    with pytest.raises(NotFound):
        service.list_notifications(state, alice_scope)


# ---- preferences -------------------------------------------------------------------


def test_preferences_round_trip_and_are_per_user(state, alice_scope, bob_scope):
    service.update_preferences(
        state,
        alice_scope,
        [Preference("signal", "webpush", False), Preference("risk", "email", True)],
    )
    service.set_quiet_hours(state, alice_scope, "22:00", "07:00")
    prefs = service.get_preferences(state, alice_scope)
    assert {(p.category, p.channel, p.enabled) for p in prefs.preferences} == {
        ("signal", "webpush", False),
        ("risk", "email", True),
    }
    assert (prefs.quiet_start, prefs.quiet_end, prefs.timezone) == ("22:00", "07:00", "UTC")
    assert "email" in prefs.channels and "webpush" in prefs.channels
    other = service.get_preferences(state, bob_scope)
    assert other.preferences == () and other.quiet_start is None
    audit = state.sql("SELECT action FROM audit_log WHERE actor = ?", [alice_scope.actor])
    assert {"notify.prefs.update", "notify.quiet_hours"} <= {r["action"] for r in audit}


def test_unknown_channel_or_strategy_is_refused(state, alice_scope):
    with pytest.raises(AccountsError):
        service.update_preferences(
            state, alice_scope, [Preference("signal", "carrier_pigeon", True)]
        )
    with pytest.raises(AccountsError):
        service.update_preferences(
            state, alice_scope, [Preference("signal", "webpush", True, strategy_id="nope")]
        )


def test_quiet_hours_validation_and_clearing(state, alice_scope):
    with pytest.raises(AccountsError):
        service.set_quiet_hours(state, alice_scope, "25:00", "07:00")
    with pytest.raises(AccountsError):
        service.set_quiet_hours(state, alice_scope, "22:00", None)
    service.set_quiet_hours(state, alice_scope, "22:00", "07:00")
    service.set_quiet_hours(state, alice_scope, None, None)
    assert service.get_preferences(state, alice_scope).quiet_start is None


def test_webhook_is_validated_stored_and_never_returned_in_full(state, alice_scope):
    service.set_webhook(state, alice_scope, "https://hooks.slack.com/services/T0/B0/SECRETTOKEN")
    prefs = service.get_preferences(state, alice_scope)
    assert prefs.webhook == "https://hooks.slack.com/***"
    audit = state.sql("SELECT details_json FROM audit_log WHERE action = 'notify.webhook'")
    assert "SECRETTOKEN" not in audit[0]["details_json"]
    for bad in (
        "http://hooks.example/x",
        "https://10.0.0.5/hook",
        "https://localhost/x",
        "ftp://x",
        # AS-05: numeric shorthand for loopback and link-local.
        "https://127.1/x",
        "https://2130706433/x",
        "https://0x7f000001/x",
        "https://0x7f.1/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://169.254.169.254/latest",
    ):
        with pytest.raises(AccountsError):
            service.set_webhook(state, alice_scope, bad)
    service.set_webhook(state, alice_scope, None)
    assert service.get_preferences(state, alice_scope).webhook is None


# ---- feed ------------------------------------------------------------------------


@pytest.fixture
def router(state, clock):
    return NotificationRouter(state, {}, clock=clock)


def _notify(router, user_id, title):
    router.publish(
        Event(
            category="order",
            title=title,
            body="x",
            audience=Audience.users(user_id),
            deep_link="/orders",
        )
    )


def test_feed_lists_only_my_notifications_newest_first(state, users, router, alice_scope):
    _notify(router, users["alice"].id, "a1")
    _notify(router, users["bob"].id, "b1")
    _notify(router, users["alice"].id, "a2")
    # A legacy admin-audience alert (user_id NULL) isn't in anyone's feed.
    state.execute(
        "INSERT INTO alerts (level, title, message, created_at) VALUES ('error', 'g', 'g', 'x')"
    )
    feed = service.list_notifications(state, alice_scope)
    assert [n.title for n in feed] == ["a2", "a1"]
    assert feed[0].deep_link == "/orders" and feed[0].category == "order"
    assert service.unread_count(state, alice_scope) == 2


def test_feed_paging_and_unread_filter(state, users, router, alice_scope):
    for i in range(5):
        _notify(router, users["alice"].id, f"n{i}")
    page = service.list_notifications(state, alice_scope, limit=2)
    assert [n.title for n in page] == ["n4", "n3"]
    nxt = service.list_notifications(state, alice_scope, limit=2, before_id=page[-1].id)
    assert [n.title for n in nxt] == ["n2", "n1"]
    service.mark_read(state, alice_scope, [page[0].id])
    unread = service.list_notifications(state, alice_scope, unread_only=True)
    assert "n4" not in [n.title for n in unread]
    with pytest.raises(AccountsError):
        service.list_notifications(state, alice_scope, limit=0)


def test_mark_read_ignores_other_users_ids(state, users, router, alice_scope, bob_scope):
    _notify(router, users["alice"].id, "a1")
    _notify(router, users["bob"].id, "b1")
    [bobs] = service.list_notifications(state, bob_scope)
    assert service.mark_read(state, alice_scope, [bobs.id]) == 0
    assert service.unread_count(state, bob_scope) == 1
    assert service.mark_read(state, alice_scope) == 1  # all of mine
    assert service.unread_count(state, alice_scope) == 0
    assert service.mark_read(state, alice_scope) == 0  # already read


# ---- the console's contract (web/src/app/core/pwa/push-subscription-api.ts) -------


def test_register_from_the_browsers_subscription_json(state, alice_scope):
    p256dh, auth = _keys()
    data = {"endpoint": ENDPOINT, "expirationTime": None, "keys": {"p256dh": p256dh, "auth": auth}}
    first = service.register_browser_subscription(state, alice_scope, data, user_agent="Firefox")
    again = service.register_browser_subscription(state, alice_scope, data, user_agent="Firefox")
    assert first.id == again.id  # idempotent per endpoint
    assert len(service.list_push_subscriptions(state, alice_scope)) == 1
    for bad in ({}, {"endpoint": ENDPOINT}, {"endpoint": ENDPOINT, "keys": "x"}, "nope"):
        with pytest.raises(AccountsError):
            service.register_browser_subscription(state, alice_scope, bad)


def test_remove_by_endpoint_is_scoped(state, alice_scope, bob_scope):
    _register(state, alice_scope)
    with pytest.raises(NotFound):
        service.remove_push_subscription_by_endpoint(state, bob_scope, ENDPOINT)
    service.remove_push_subscription_by_endpoint(state, alice_scope, ENDPOINT)
    assert service.list_push_subscriptions(state, alice_scope) == []
    with pytest.raises(NotFound):
        service.remove_push_subscription_by_endpoint(state, alice_scope, ENDPOINT)
