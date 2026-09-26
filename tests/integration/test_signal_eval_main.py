"""``python -m stonks.lab.signal_eval`` (BL-33, BL-34)."""

from __future__ import annotations

import json

from stonks.lab.signal_eval import main
from tests.fixtures.signal_research import signal_lake

TICKERS = [f"T{i:02d}" for i in range(10)]


def test_main_writes_json_and_html(tmp_path, capsys):
    lake_path = tmp_path / "lake.duckdb"
    signal_lake(lake_path, TICKERS, periods=120, seed=8).close()
    out_json, out_html = tmp_path / "ic.json", tmp_path / "ic.html"
    code = main(
        [
            "--strategy",
            "momentum",
            "--params",
            '{"lookback_days": 10, "threshold": -1.0}',
            "--tickers",
            ",".join(TICKERS),
            "--lake",
            str(lake_path),
            "--horizons",
            "1,5",
            "--every-bars",
            "2",
            "--events",
            "--workers",
            "1",
            "--json",
            str(out_json),
            "--html",
            str(out_html),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "momentum: 10 tickers" in printed
    assert "event study momentum" in printed
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["ic"]["status"] == "ok"
    assert [h["horizon"] for h in data["ic"]["horizons"]] == [1, 5]
    assert data["events"]["n_events"] >= 0
    html = out_html.read_text(encoding="utf-8")
    assert "Signal IC" in html and "Event study" in html
