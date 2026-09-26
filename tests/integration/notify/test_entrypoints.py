"""``python -m stonks.notify`` (VAPID keygen, test notification, one delivery
pass) and the OutboxNotifier bridge from the existing Notifier seam."""

from __future__ import annotations

import base64

import pytest

from stonks.notify import Notification, OutboxNotifier
from stonks.notify.__main__ import main
from stonks.store.state import SqliteState

from .fakes import FakeChannel


def test_vapid_keygen_prints_env_lines(capsys):
    assert main(["vapid-keygen"]) == 0
    out = capsys.readouterr().out.splitlines()
    pub = next(line for line in out if line.startswith("STONKS_VAPID_PUBLIC_KEY="))
    priv = next(line for line in out if line.startswith("STONKS_VAPID_PRIVATE_KEY="))
    raw_pub = pub.split("=", 1)[1]
    raw_priv = priv.split("=", 1)[1]
    assert len(base64.urlsafe_b64decode(raw_pub + "==")) == 65
    assert len(base64.urlsafe_b64decode(raw_priv + "==")) == 32


@pytest.fixture
def state_path(tmp_path, users, state):
    # ``users`` created Alice etc. in the ``state`` fixture's DB.
    return tmp_path / "state.sqlite"


@pytest.fixture
def fake(monkeypatch):
    channel = FakeChannel("webhook", default_enabled=True)
    monkeypatch.setattr("stonks.notify.__main__.build_channels", lambda s: {"webhook": channel})
    return channel


def test_send_a_test_notification_by_email(state_path, users, fake, capsys):
    code = main(["test", "--user", "alice@example.com", "--state", str(state_path)])
    assert code == 0
    [(msg, _)] = fake.sent
    assert msg.user_id == users["alice"].id and msg.title == "Test notification"
    out = capsys.readouterr().out
    assert "sent=1" in out


def test_test_notification_can_be_urgent_and_by_id(state_path, users, fake):
    assert main(["test", "--user", users["bob"].id, "--urgent", "--state", str(state_path)]) == 0
    assert fake.sent[0][0].urgency == "high"


def test_unknown_user_fails_cleanly(state_path, fake, capsys):
    assert main(["test", "--user", "nobody@example.com", "--state", str(state_path)]) == 2
    assert "not found" in capsys.readouterr().err


def test_deliver_runs_one_pass(state_path, fake, capsys):
    assert main(["deliver", "--state", str(state_path)]) == 0
    assert "sent=0" in capsys.readouterr().out


def test_outbox_notifier_reaches_every_admin(state_path, users):
    hook = FakeChannel("webhook", default_enabled=True)
    notifier = OutboxNotifier(state_path, {"webhook": hook})
    notifier.notify(
        Notification(level="error", title="tick failed", message="see logs", fields={"x": 1})
    )
    notifier.notify(
        Notification(
            level="warning", title="stale data", message="m", fields={"dedupe_key": "stale:1"}
        )
    )
    notifier.notify(
        Notification(
            level="warning", title="stale data", message="m", fields={"dedupe_key": "stale:1"}
        )
    )
    with SqliteState(state_path) as s:
        rows = s.sql("SELECT user_id, category, urgency, title FROM notification_outbox")
    admins = {"usr_owner", users["admin"].id}
    assert {r["user_id"] for r in rows} == admins
    assert len(rows) == 4  # two events x two admins; the repeat was deduped
    assert {r["category"] for r in rows} == {"system"}
    assert {r["urgency"] for r in rows if r["title"] == "tick failed"} == {"high"}
