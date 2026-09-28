"""The grounding check inside the agent loop (roadmap 23.8)."""

from __future__ import annotations

from stonks.assistant.fake import Script, call
from stonks.assistant.grounding import GroundingSettings
from tests.unit.assistant.test_loop import OWNER, _loop, _run, kinds, store  # noqa: F401


def _answer(conv_store, conv_id: str) -> str:
    return conv_store.messages(conv_id)[-1].content


def test_a_grounded_reply_passes_untouched(store):  # noqa: F811
    conv = store.create(OWNER)
    loop, _, _ = _loop(
        store, [Script(calls=(call("get_portfolio"),)), Script(text="You have 1,000 in cash.")]
    )
    events = _run(loop.send(conv.id, "cash?"))
    assert "grounding" not in kinds(events)
    assert _answer(store, conv.id) == "You have 1,000 in cash."


def test_flag_mode_adds_a_note_naming_the_numbers(store):  # noqa: F811
    conv = store.create(OWNER)
    loop, _, _ = _loop(
        store,
        [Script(calls=(call("get_portfolio"),)), Script(text="Cash is 1,000 and Sharpe 1.87.")],
    )
    events = _run(loop.send(conv.id, "how is it going?"))
    [flag] = [e for e in events if e.kind == "grounding"]
    assert flag.data["status"] == "flagged" and flag.data["ungrounded"] == ["1.87"]
    assert "1.87" in _answer(store, conv.id) and "Check these numbers" in _answer(store, conv.id)
    streamed = "".join(e.data["delta"] for e in events if e.kind == "text")
    assert streamed.endswith(_answer(store, conv.id).split("Sharpe 1.87.")[1])


def test_numbers_from_an_earlier_turn_do_not_count(store):  # noqa: F811
    conv = store.create(OWNER)
    loop, _, _ = _loop(store, [Script(calls=(call("get_portfolio"),)), Script(text="ok")])
    _run(loop.send(conv.id, "cash?"))
    loop2, _, _ = _loop(store, [Script(text="Still 1,000 in cash.")])
    events = _run(loop2.send(conv.id, "and now?"))
    assert [e.data["status"] for e in events if e.kind == "grounding"] == ["flagged"]


def test_rewrite_mode_asks_once_and_keeps_the_cited_answer(store):  # noqa: F811
    conv = store.create(OWNER)
    loop, model, _ = _loop(
        store,
        [
            Script(calls=(call("get_portfolio"),)),
            Script(text="Cash is 1,000 and Sharpe 1.87."),
            Script(text="Cash is 1,000 (get_portfolio)."),
        ],
        grounding=GroundingSettings(mode="rewrite"),
    )
    events = _run(loop.send(conv.id, "how is it going?"))
    statuses = [e.data["status"] for e in events if e.kind == "grounding"]
    assert statuses == ["rewriting", "ok"]
    assert _answer(store, conv.id) == "Cash is 1,000 (get_portfolio)."
    sent, tools, _ = model.requests[-1]
    assert tools == []
    assert "1.87" in sent[-1].content and sent[-1].role == "user"
    assert [m.role for m in store.messages(conv.id)].count("user") == 1


def test_rewrite_that_still_invents_is_flagged(store):  # noqa: F811
    conv = store.create(OWNER)
    loop, _, _ = _loop(
        store,
        [
            Script(calls=(call("get_portfolio"),)),
            Script(text="Sharpe 1.87."),
            Script(text="Sharpe 1.9."),
        ],
        grounding=GroundingSettings(mode="rewrite"),
    )
    events = _run(loop.send(conv.id, "sharpe?"))
    assert [e.data["status"] for e in events if e.kind == "grounding"] == ["rewriting", "flagged"]
    assert _answer(store, conv.id).startswith("Sharpe 1.9.")


def test_off_mode_skips_the_check(store):  # noqa: F811
    conv = store.create(OWNER)
    loop, _, _ = _loop(
        store, [Script(text="Sharpe 1.87.")], grounding=GroundingSettings(mode="off")
    )
    events = _run(loop.send(conv.id, "sharpe?"))
    assert "grounding" not in kinds(events)
    assert _answer(store, conv.id) == "Sharpe 1.87."
