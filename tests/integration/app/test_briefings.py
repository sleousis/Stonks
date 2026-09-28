"""Research-only briefings (roadmap 23.8): per person, off by default,
read tools only, delivered through the notification router."""

from __future__ import annotations

from datetime import UTC, date, datetime

import anyio
import pytest
from fastapi.testclient import TestClient

from stonks.accounts import Role
from stonks.api import create_app
from stonks.app.briefings import BriefingPrefsUpdate
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.assistant import briefings
from stonks.assistant.briefings import BriefingPrefs
from stonks.assistant.fake import FakeChatModel, Script, call
from stonks.scheduling.jobs import briefing_outcome
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"
DAY = date(2026, 3, 20)


@pytest.fixture
def auth(settings, seeded):
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def world(settings, seeded, fake_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    settings.assistant.base_url = "http://model.invalid/v1"
    settings.assistant.briefings.enabled = True
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    model = FakeChatModel(
        [
            Script(calls=(call("list_halts"), call("place_order", {"ticker": "X"}))),
            Script(text="No halts. Nothing to do today."),
        ]
    )
    svc.assistant.model_factory = lambda _cfg: model
    sent: list = []
    svc.briefings._publish = sent.append
    app = create_app(settings, services=svc)
    app.state.auth = auth
    alice = add_user(settings.state.path, "alice@example.com")
    bob = add_user(settings.state.path, "bob@example.com")
    return svc, app, model, sent, alice, bob


def test_prefs_are_off_until_a_person_turns_them_on(world, settings):
    svc, _, _, _, alice, _ = world
    me = session_principal(alice, Role.TRADER)
    assert svc.briefings.prefs(me).model_dump() == {
        "available": True,
        "pre_open": False,
        "post_close": False,
    }
    view = svc.briefings.set_prefs(me, BriefingPrefsUpdate(pre_open=True, post_close=False))
    assert view.pre_open and not view.post_close
    with SqliteState(settings.state.path) as state:
        assert briefings.subscribers(state, "pre_open") == [alice]
        assert briefings.subscribers(state, "post_close") == []


def test_a_run_briefs_only_who_asked_with_read_tools_only(world, settings):
    svc, app, model, sent, alice, bob = world
    with SqliteState(settings.state.path) as state:
        briefings.set_prefs(state, alice, BriefingPrefs(pre_open=True))
        briefings.set_prefs(state, bob, BriefingPrefs(post_close=True))
        orders_before = state.count_rows("orders")
    view = anyio.run(svc.briefings.run, app, "pre_open", DAY)
    assert (view.people, view.sent, view.failed, view.skipped) == (1, 1, 0, None)
    [event] = sent
    assert event.audience.user_ids == (alice,)
    assert event.category == "system" and event.dedupe_key == "briefing:pre_open:2026-03-20"
    assert event.body.startswith("No halts.")
    # the model saw read tools only; the write it asked for never ran
    _, tools, _ = model.requests[0]
    assert "list_halts" in tools and "place_order" not in tools
    assert not any(t.startswith(("run_", "create_", "place_")) for t in tools)
    with SqliteState(settings.state.path) as state:
        [conv] = state.sql("SELECT * FROM assistant_conversations WHERE owner_id = ?", [alice])
        assert conv["research_only"] == 1
        assert state.count_rows("orders") == orders_before


def test_off_by_default_skips_the_run(world, settings):
    svc, app, _, sent, _, _ = world
    settings.assistant.briefings.enabled = False
    view = anyio.run(svc.briefings.run, app, "post_close", DAY)
    assert view.skipped == "briefings are off" and sent == []
    assert briefing_outcome(view.model_dump(mode="json")).status == "skipped"


def test_prefs_routes(world):
    _, app, _, _, alice, _ = world
    auth = app.state.auth
    _, token = auth.create_token(
        session_principal(alice, Role.TRADER), name="t", scopes=["read", "trade"]
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        assert c.get("/api/assistant/briefings/prefs", headers=headers).json()["pre_open"] is False
        put = c.put(
            "/api/assistant/briefings/prefs",
            json={"pre_open": True, "post_close": True},
            headers=headers,
        )
        assert put.status_code == 200 and put.json()["post_close"] is True
        run = c.post("/api/assistant/briefings/run", json={"kind": "pre_open"}, headers=headers)
        assert run.status_code == 403
