"""Static HTML report (roadmap 4.1)."""

from __future__ import annotations

import json
import re
from datetime import date

import pytest

from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.reporting import build_report, render_html
from stonks.reporting.charts import line_chart
from tests.paper_seed import (
    TICK_ID,
    days,
    make_env,
    oos,
    register,
    seed_fills,
    seed_portfolio,
    seed_shadow,
)

EVIL = "<script>alert('pwn')</script>"
POLICY = GoLivePolicy(min_days=5, max_drawdown=0.2, max_drift=0.05, min_trades=1)


@pytest.fixture
def env(tmp_path):
    state, registry = make_env(tmp_path)
    yield state, registry
    state.close()


@pytest.fixture
def seeded(env):
    state, registry = env
    active = register(
        registry,
        "active",
        [oos(0.2), SurvivalReport(test_id="perturbation", passed=False, metrics={"x": 1.0})],
        strategy_id="alpha",
    )
    shadow = register(registry, "shadow", [oos(0.0)], strategy_id="beta")
    ds = days(10)
    values = [10_000, 10_500, 9_800, 10_200, 10_900, 11_000, 10_700, 11_200, 11_500, 11_400]
    seed_portfolio(state, list(zip(ds, values, strict=True)), positions={"UP.US": 42.0})
    seed_fills(state, active, 3, day=ds[1])
    seed_shadow(state, shadow, [(d, 10_000.0 + 10 * i) for i, d in enumerate(ds)], fills=2)
    # A hostile strategy row: ids, params and notes are all user-controlled.
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, artifact_path, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'shadow', ?, ?)",
        [
            EVIL,
            f"evil.module:{EVIL}",
            json.dumps({"k": EVIL}),
            "x",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ],
    )
    state.execute(
        "INSERT INTO survival_reports (strategy_id, test_id, passed, metrics_json, notes, "
        "created_at) VALUES (?, ?, 1, ?, ?, ?)",
        [EVIL, EVIL, json.dumps({EVIL: 1.0}), EVIL, "2026-01-01T00:00:00+00:00"],
    )
    state.execute(
        "INSERT INTO shadow_portfolio_snapshots (tick_id, strategy_id, as_of, taken_at, cash, "
        "positions_json, total_value) VALUES (?, ?, '2026-01-01', '2026-01-01T21:00:00', 1, "
        "?, 1)",
        [TICK_ID, EVIL, json.dumps({EVIL: 1})],
    )
    return state, registry


def _html(state, registry, **kw):
    return render_html(build_report(state, registry, POLICY, **kw))


def test_report_renders_seeded_state(seeded):
    html = _html(*seeded)

    assert html.startswith("<!doctype html>")
    assert "prefers-color-scheme: dark" in html
    assert html.count("<svg") >= 3  # equity, drawdown, per-strategy paper charts
    assert "UP.US" in html and "42" in html  # position
    assert "alpha" in html and "beta" in html
    assert "perturbation" in html
    assert "FAIL" in html and "PASS" in html  # survival verdicts / gate checks
    assert "drift" in html.lower()
    assert "+14.00%" in html  # portfolio cumulative return 11400/10000


def test_report_escapes_hostile_strategy_data(seeded):
    html = _html(*seeded)

    assert "<script" not in html
    assert "alert('pwn')" not in html
    assert "&lt;script&gt;" in html


def test_report_makes_no_external_requests(seeded):
    html = _html(*seeded)

    assert re.findall(r"https?://", html) == []
    assert not re.search(r"""(?:src|href|url)\s*[=(]\s*["']?//""", html)
    assert "<link" not in html and "<img" not in html and "@import" not in html


def test_report_on_empty_state_renders_placeholders(env):
    html = _html(*env)

    assert "no portfolio snapshots" in html
    assert "no strategies" in html


def test_report_filters_strategies_and_flags_unknown_ids(seeded):
    html = _html(*seeded, strategy_ids=["beta", "ghost"])

    assert 'id="strategy-beta"' in html
    assert 'id="strategy-alpha"' not in html
    assert "ghost" in html  # reported as unknown rather than silently dropped


def test_report_since_trims_equity_rows(seeded):
    data = build_report(*seeded, POLICY, since=date(2026, 1, 8))
    assert [r.day for r in data.portfolio] == [
        date(2026, 1, 8),
        date(2026, 1, 9),
        date(2026, 1, 10),
    ]


def test_line_chart_handles_empty_single_and_flat_series():
    assert "no data" in line_chart([])
    one = line_chart([("v", [(date(2026, 1, 1), 5.0)], "s1")])
    flat = line_chart([("v", [(date(2026, 1, 1), 5.0), (date(2026, 1, 2), 5.0)], "s1")])
    for svg in (one, flat):
        assert "<svg" in svg and "nan" not in svg.lower() and "inf" not in svg.lower()


def test_line_chart_escapes_labels():
    svg = line_chart([(EVIL, [(date(2026, 1, 1), 1.0), (date(2026, 1, 2), 2.0)], EVIL)])
    assert "<script" not in svg
