"""The log line contract: one JSON object per event with the level, a UTC ISO
timestamp, bound and context variables, and a formatted traceback. Guards the
structlog processors across library upgrades."""

from __future__ import annotations

import json

import structlog

from stonks.logging import configure_logging


def _lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_event_renders_as_one_json_line(capsys):
    configure_logging(level="INFO")
    try:
        structlog.contextvars.bind_contextvars(tick_id="t1")
        log = structlog.get_logger("stonks.test").bind(run_id="r1")
        log.info("tick_done", orders=3)
        log.debug("hidden")
    finally:
        structlog.contextvars.clear_contextvars()
    rows = _lines(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "tick_done"
    assert row["level"] == "info"
    assert row["orders"] == 3
    assert row["run_id"] == "r1"
    assert row["tick_id"] == "t1"
    assert row["timestamp"].endswith("Z")


def test_exception_is_formatted_into_the_line(capsys):
    configure_logging(level="INFO")
    log = structlog.get_logger("stonks.test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")
    row = _lines(capsys.readouterr().out)[0]
    assert row["level"] == "error"
    assert "ValueError: boom" in row["exception"]
