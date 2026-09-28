"""Telegram bot end to end (roadmap 20.3): link codes through the API, the
bot answering as the linked user with a fake Telegram API, the kill switch
with its typed confirmation, and tenant isolation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from stonks.accounts import PortfolioRepository, Scope
from stonks.store.state import SqliteState
from stonks.telegram.bot import HostedTelegramBot, TelegramBot
from stonks.telegram.commands import KILL_PHRASE, CommandHandler
from stonks.telegram.fake import FakeTelegramApi
from stonks.telegram.settings import TelegramConfig
from tests.integration.app.stepup import allow_step_up


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("STONKS_TELEGRAM_BOT_TOKEN", "1:test-token")


@pytest.fixture
def books(settings, people) -> dict[str, str]:
    """A portfolio each for Alice and Bob; Alice's holds ALICEONLY.US."""
    out: dict[str, str] = {}
    now = datetime.now(UTC).isoformat()
    with SqliteState(settings.state.path) as state:
        for name in ("alice", "bob"):
            p = people[name]
            pid = (
                PortfolioRepository(state)
                .create(Scope(user_id=p["id"], role=p["role"]), name=f"{name} book")
                .id
            )
            out[name] = pid
            positions = {"ALICEONLY.US": 3.0} if name == "alice" else {}
            state.execute(
                "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
                " total_value, portfolio_id) VALUES (NULL, ?, ?, 1000, ?, 1000, ?)",
                [now[:10], now, json.dumps(positions), pid],
            )
    return out


def _code(client, people, name: str) -> str:
    allow_step_up(client.app)  # a code is minted from a signed-in browser session
    resp = client.post("/api/telegram/link-code", headers=people[name]["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()["code"]


def _bot(app) -> tuple[TelegramBot, FakeTelegramApi]:
    api = FakeTelegramApi()
    context = app.state.services.context
    return TelegramBot(api, context, TelegramConfig(enabled=True)), api


def test_link_status_code_and_unlink_through_the_api(client, people, app):
    alice = people["alice"]["headers"]
    status = client.get("/api/telegram/link", headers=alice).json()
    assert status["bot_configured"] is True and status["linked"] is False
    code = _code(client, people, "alice")
    bot, api = _bot(app)
    api.push(100, f"/link {code}", username="alice_tg")
    bot.poll_once(timeout=0)
    assert api.replies(100)[-1].startswith("Linked.")
    status = client.get("/api/telegram/link", headers=alice).json()
    assert status["linked"] is True and status["username"] == "alice_tg"
    assert client.delete("/api/telegram/link", headers=alice).status_code == 204
    assert client.get("/api/telegram/link", headers=alice).json()["linked"] is False


def test_viewer_cannot_make_a_code_and_no_token_is_503(client, people, monkeypatch):
    assert client.post("/api/telegram/link-code", headers=people["vic"]["headers"]).status_code == (
        403
    )
    monkeypatch.delenv("STONKS_TELEGRAM_BOT_TOKEN")
    allow_step_up(client.app)
    resp = client.post("/api/telegram/link-code", headers=people["alice"]["headers"])
    assert resp.status_code == 503 and resp.json()["code"] == "not_configured"


def test_unlinked_chat_only_gets_help(client, people, app):
    bot, api = _bot(app)
    api.push(5, "/positions")
    api.push(5, "/link WRONGCODE")
    bot.poll_once(timeout=0)
    replies = api.replies(5)
    assert "not linked" in replies[0] and "unknown" in replies[1]


def test_commands_act_as_the_linked_user_only(client, people, app, books):
    bot, api = _bot(app)
    api.push(1, f"/link {_code(client, people, 'alice')}")
    api.push(2, f"/link {_code(client, people, 'bob')}")
    bot.poll_once(timeout=0)
    api.push(1, "/positions")
    api.push(2, "/positions")
    api.push(2, f"/positions {books['alice']}")
    api.push(2, "/status")
    api.push(2, "/today")
    api.push(2, "/signals")
    bot.poll_once(timeout=0)
    alice_says = api.replies(1)[-1]
    bob_says = api.replies(2)
    assert "ALICEONLY.US" in alice_says
    assert all("ALICEONLY" not in text for text in bob_says)
    assert "not found" in bob_says[-4].lower()
    assert "No halt in force" in bob_says[-3]
    assert "Today" in bob_says[-2]
    assert "follow no strategy" in bob_says[-1]


def test_kill_needs_the_typed_confirmation(client, people, app, books, settings):
    bot, api = _bot(app)
    api.push(1, f"/link {_code(client, people, 'alice')}")
    api.push(1, "/kill")
    api.push(1, "kill all please")
    bot.poll_once(timeout=0)
    assert "Type KILL ALL" in api.replies(1)[-2]
    assert api.replies(1)[-1] == "Kill switch cancelled."
    api.push(1, "/kill")
    api.push(1, KILL_PHRASE)
    bot.poll_once(timeout=0)
    assert api.replies(1)[-1].startswith("Kill switch on")
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT scope, user_id, kind, halt FROM risk_halts")
    assert [tuple(r) for r in rows] == [("user", people["alice"]["id"], "kill", "all")]


def test_viewer_kill_is_refused(client, people, app, auth, settings):
    from stonks.telegram.links import LinkStore

    with SqliteState(settings.state.path) as state:
        store = LinkStore(state)
        code, _ = store.create_code(people["vic"]["id"], minutes=5, actor="t")
    bot, api = _bot(app)
    api.push(9, f"/link {code}")
    api.push(9, "/kill")
    api.push(9, KILL_PHRASE)
    bot.poll_once(timeout=0)
    assert api.replies(9)[1] == "Your role cannot use the kill switch."
    with SqliteState(settings.state.path) as state:
        assert state.sql("SELECT COUNT(*) FROM risk_halts")[0][0] == 0


def test_kill_confirmation_expires(client, people, app, books):
    clock = [datetime.now(UTC)]
    context = app.state.services.context
    handler = CommandHandler(
        context, TelegramConfig(kill_confirm_minutes=1), clock=lambda: clock[0]
    )
    api = FakeTelegramApi()
    bot = TelegramBot(api, context, handler=handler)
    api.push(1, f"/link {_code(client, people, 'alice')}")
    bot.poll_once(timeout=0)
    api.push(1, "/kill")
    bot.poll_once(timeout=0)
    clock[0] += timedelta(minutes=2)
    api.push(1, KILL_PHRASE)
    bot.poll_once(timeout=0)
    assert "expired" in api.replies(1)[-1]


def test_offset_is_stored_so_a_restart_never_answers_twice(client, people, app):
    bot, api = _bot(app)
    api.push(3, "/help")
    bot.poll_once(timeout=0)
    assert len(api.sent) == 1
    fresh = TelegramBot(api, app.state.services.context)
    fresh.poll_once(timeout=0)
    assert len(api.sent) == 1
    assert api.offsets[-1] == 2


def test_one_bad_update_does_not_stop_the_loop(client, people, app, monkeypatch):
    bot, api = _bot(app)
    calls = {"n": 0}
    real = bot._handler.handle

    def flaky(update):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(update)

    monkeypatch.setattr(bot._handler, "handle", flaky)
    api.push(3, "/help")
    api.push(4, "/help")
    assert bot.poll_once(timeout=0) == 2
    assert api.replies(4) and not api.replies(3)


def test_group_chats_are_refused(client, people, app):
    bot, api = _bot(app)
    api.push(-50, "/status", chat_type="group")
    bot.poll_once(timeout=0)
    assert api.replies(-50) == ["Stonks only answers private chats."]


def test_hosted_bot_runs_only_when_enabled(app):
    context = app.state.services.context
    assert HostedTelegramBot(context, TelegramConfig(enabled=False)).start() is False
    api = FakeTelegramApi()
    hosted = HostedTelegramBot(
        context,
        TelegramConfig(enabled=True, poll_timeout_seconds=0, error_backoff_seconds=0.01),
        bot=TelegramBot(api, context, TelegramConfig(poll_timeout_seconds=0)),
    )
    api.push(8, "/help")
    assert hosted.start() is True and hosted.running
    for _ in range(200):
        if api.sent:
            break
        import time

        time.sleep(0.01)
    hosted.stop()
    assert api.replies(8) and not hosted.running


def test_an_api_token_cannot_make_a_link_code(client, people):
    # a leaked token must not link an attacker's chat, which then acts as
    # the person with their full role (review 2026-09-27)
    resp = client.post("/api/telegram/link-code", headers=people["alice"]["headers"])
    assert resp.status_code == 403
