"""The daily loop end to end through the CLI, compared with a golden file.

On a fresh data folder: ``db init``, ingest metadata and prices from the
canned source (a split, a dividend, a benchmark, a crypto ticker), a lab run
that registers the fitted strategy, ``registry promote --override``, a real
paper tick and the HTML report. Key outputs are compared with
``tests/fixtures/golden/cli_golden_run.json`` within tolerances.

Refresh the golden file after an intended change with
``STONKS_UPDATE_GOLDEN=1 uv run pytest tests/integration/test_cli_golden_run.py``.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from tests.e2e.fake_market import TICKERS, CannedDataSource, build_market

GOLDEN = Path(__file__).parents[1] / "fixtures" / "golden" / "cli_golden_run.json"
MARKET_END = date(2026, 6, 30)
LAB_START, LAB_END = "2025-07-01", "2026-06-26"
TICK_DAY = "2026-06-30"
REL, ABS = 1e-6, 1e-6


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        '[lake]\npath = "data/lake.duckdb"\n\n[state]\npath = "data/state.sqlite"\n\n'
        '[registry]\nartifacts_dir = "data/artifacts"\n\n'
        '[production]\ninitial_cash = 10000.0\n\n[lab]\nbenchmark = "SPY.US"\n',
        encoding="utf-8",
    )
    market = build_market(MARKET_END)
    # Every source id builds the canned source: nothing reaches a vendor.
    import stonks.ingest.sources.registry as registry

    for source_id in list(registry._FACTORIES):
        monkeypatch.setitem(registry._FACTORIES, source_id, lambda _cfg: CannedDataSource(market))
    return tmp_path


def _invoke(*args: str) -> str:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == 0, f"stonks {' '.join(args)}\n{result.output}"
    return result.output


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if isinstance(value, dict):
        return {k: _round(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_round(v) for v in value]
    return value


def _run_loop(home: Path) -> dict[str, Any]:
    tickers = ",".join(TICKERS)
    _invoke("db", "init")
    _invoke("ingest", "metadata", "--tickers", tickers)
    _invoke("ingest", "prices", "--tickers", tickers, "--since", "2024-01-01")

    lake = DuckDBLake(home / "data" / "lake.duckdb")
    try:
        bars = {
            r[0]: r[1]
            for r in lake.con.execute(
                "SELECT ticker, COUNT(*) FROM prices GROUP BY ticker ORDER BY ticker"
            ).fetchall()
        }
        splits = lake.con.execute("SELECT ticker, date, ratio FROM stock_splits").fetchall()
        dividends = lake.con.execute("SELECT ticker, ex_date, amount FROM dividends").fetchall()
    finally:
        lake.close()

    lab_json = home / "lab.json"
    lab_out = _invoke(
        "lab",
        "run",
        "buy_and_hold",
        "--params",
        '{"ticker": "AAA.US", "allocation": 1.0}',
        "--tickers",
        "AAA.US",
        "--start",
        LAB_START,
        "--end",
        LAB_END,
        "--tests",
        "oos",
        # Buy and hold makes one trade: judge it on the flat Sharpe gate.
        "--test-option",
        "oos.mode=sharpe",
        "--test-option",
        "oos.min_trades=0",
        "--register",
        "--workers",
        "1",
        "--hypothesis",
        "Holding a steadily rising stock earns its drift; the canned AAA.US path rises.",
        "--json-out",
        str(lab_json),
    )
    lab = json.loads(lab_json.read_text(encoding="utf-8"))
    sid = lab["registered_id"]
    assert sid and f"registered {sid}" in lab_out, lab_out

    promote_out = _invoke(
        "registry", "promote", sid, "--override", "--reason", "Golden run promotes on purpose."
    )
    tick_out = _invoke("tick", "--as-of", TICK_DAY, "--tickers", "AAA.US")
    tick = dict(re.findall(r"(\w+)=(\S+)", tick_out))
    report_path = home / "report.html"
    _invoke("report", "--out", str(report_path))
    report_html = report_path.read_text(encoding="utf-8")

    with SqliteState(home / "data" / "state.sqlite") as state:
        status = state.sql("SELECT status FROM strategies WHERE id = ?", [sid])[0]["status"]
        change = state.sql(
            "SELECT to_status, override FROM status_changes WHERE strategy_id = ?"
            " ORDER BY id DESC LIMIT 1",
            [sid],
        )[0]
        snap = state.sql(
            "SELECT cash, total_value, positions_json FROM portfolio_snapshots"
            " ORDER BY id DESC LIMIT 1"
        )[0]
        fills = state.sql("SELECT ticker, quantity, price, fee FROM fills ORDER BY id")

    return _round(
        {
            "ingest": {
                "bars": bars,
                "splits": [[t, str(d), r] for t, d, r in splits],
                "dividends": [[t, str(d), a] for t, d, a in dividends],
            },
            "lab": {
                "verdict": lab["verdict"],
                "n_trials_run": lab["n_trials_run"],
                "best_params": lab["best_params"],
                "benchmark": lab["benchmark"],
                "survival": {
                    r["test_id"]: [r["passed"], r["metrics"]] for r in lab["survival_reports"]
                },
                "registered": True,
            },
            "promote": {
                "printed": "active" in promote_out,
                "status": status,
                "to_status": change["to_status"],
                "override": bool(change["override"]),
            },
            "tick": {
                "status": tick["status"],
                "winner": tick["winner"] == sid,
                "orders": int(tick["orders"]),
                "fills": int(tick["fills"]),
                "fill_rows": [[f["ticker"], f["quantity"], f["price"], f["fee"]] for f in fills],
                "cash": snap["cash"],
                "total_value": snap["total_value"],
                "positions": json.loads(snap["positions_json"]),
            },
            "report": {"mentions_strategy": sid in report_html},
        }
    )


def _compare(actual: Any, expected: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        diffs = [f"{path}.{k}: missing" for k in expected.keys() - actual.keys()]
        diffs += [f"{path}.{k}: unexpected" for k in actual.keys() - expected.keys()]
        for key in expected.keys() & actual.keys():
            diffs += _compare(actual[key], expected[key], f"{path}.{key}")
        return diffs
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(actual)} != {len(expected)}"]
        return [
            d
            for i, (a, e) in enumerate(zip(actual, expected, strict=True))
            for d in _compare(a, e, f"{path}[{i}]")
        ]
    if (
        isinstance(expected, float)
        and isinstance(actual, int | float)
        and not isinstance(actual, bool)
    ):
        ok = math.isclose(actual, expected, rel_tol=REL, abs_tol=ABS)
        return [] if ok else [f"{path}: {actual!r} != {expected!r}"]
    return [] if actual == expected else [f"{path}: {actual!r} != {expected!r}"]


def test_cli_golden_run(cli_home):
    actual = _run_loop(cli_home)
    if os.environ.get("STONKS_UPDATE_GOLDEN") == "1" or not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"wrote {GOLDEN.name}; check it in")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    diffs = _compare(actual, expected)
    assert not diffs, "golden run drifted:\n" + "\n".join(diffs)
