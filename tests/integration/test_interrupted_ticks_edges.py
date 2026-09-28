"""Recovering interrupted ticks at the edges (TO-06, BE-44): another live
process on this host keeps its tick, a finished one loses it, and rows
with an unreadable owner or start time are judged safely."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import timedelta

from stonks.production.tick import recover_interrupted_ticks
from tests.integration.test_interrupted_ticks import NOW, _owned, _row


def test_a_tick_of_another_live_process_on_this_host_survives(state):
    here = socket.gethostname()
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    try:
        _owned(state, "tick_alive", NOW - timedelta(minutes=5), here, alive.pid)
        _owned(state, "tick_done", NOW - timedelta(minutes=5), here, done.pid)
        assert recover_interrupted_ticks(state, now=NOW) == ["tick_done"]
        assert _row(state, "tick_alive")["status"] == "running"
    finally:
        alive.kill()
        alive.wait()
    assert recover_interrupted_ticks(state, now=NOW) == ["tick_alive"]


def _raw(state, tick_id, started_at, summary):
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status, summary_json) VALUES (?, ?, 'running', ?)",
        [tick_id, started_at, summary],
    )


def test_an_unreadable_owner_is_taken_for_gone(state):
    _raw(state, "tick_bad_json", NOW.isoformat(), "{not json")
    _raw(state, "tick_list", NOW.isoformat(), "[1, 2]")
    assert sorted(recover_interrupted_ticks(state, now=NOW)) == ["tick_bad_json", "tick_list"]


def test_another_hosts_tick_is_judged_by_its_start_time(state):
    owner = json.dumps({"owner": {"host": "other-host", "pid": 1}})
    _raw(state, "tick_bad_time", "yesterday-ish", owner)
    # a naive start time is read as UTC: 11 hours old is still young
    young = (NOW - timedelta(hours=11)).replace(tzinfo=None).isoformat()
    _raw(state, "tick_naive_young", young, owner)
    assert recover_interrupted_ticks(state, now=NOW) == ["tick_bad_time"]
    assert _row(state, "tick_naive_young")["status"] == "running"
