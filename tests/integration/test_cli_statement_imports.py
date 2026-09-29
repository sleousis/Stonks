"""``stonks imports`` (roadmap 23.17): preview, commit, list and undo."""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from stonks.cli import app

CSV = "Date,Action,Symbol,Quantity,Price,Amount\n2026-01-05,BUY,AAPL,10,190,-1900\n"
MAPPING = json.dumps(
    {
        "date": "Date",
        "type": "Action",
        "symbol": "Symbol",
        "quantity": "Quantity",
        "price": "Price",
        "amount": "Amount",
        "types": {"BUY": "trade"},
    }
)


def test_preview_commit_list_undo(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "300")
    csv_file = tmp_path / "jan.csv"
    csv_file.write_text(CSV, encoding="utf-8")
    runner = CliRunner()
    args = [str(csv_file), "--mapping", MAPPING, "--new", "Old broker"]
    shown = runner.invoke(app, ["imports", "preview", *args])
    assert shown.exit_code == 0, shown.output
    assert "1 new, 0 duplicate, 0 skipped" in shown.output and "AAPL.US" in shown.output
    done = runner.invoke(app, ["imports", "commit", *args])
    assert done.exit_code == 0, done.output
    assert "1 added" in done.output
    import_id = re.search(r"imported (imp_[0-9a-f]+)", done.output).group(1)
    listed = runner.invoke(app, ["imports", "list"])
    assert import_id in listed.output and "jan.csv" in listed.output
    again = runner.invoke(app, ["imports", "commit", *args[:3], "--new", "Other"])
    assert again.exit_code != 0  # every row is a duplicate
    undone = runner.invoke(app, ["imports", "undo", import_id])
    assert undone.exit_code == 0 and "1 rows removed" in undone.output, undone.output
    bad = runner.invoke(app, ["imports", "preview", str(csv_file), "--mapping", "{", "--new", "x"])
    assert bad.exit_code != 0


def test_degiro_presets_from_the_cli(tmp_path, monkeypatch):
    """A DEGIRO export needs no mapping: found from its headers, or named
    with --preset. A Portfolio export takes its day with --as-of."""
    from pathlib import Path

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "degiro"
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "300")
    runner = CliRunner()
    listed = runner.invoke(app, ["imports", "presets"])
    assert listed.exit_code == 0, listed.output
    assert "degiro_transactions" in listed.output and "degiro_portfolio" in listed.output
    tx = str(fixtures / "transactions_nl.csv")
    shown = runner.invoke(app, ["imports", "preview", tx, "--new", "DEGIRO", "--currency", "EUR"])
    assert shown.exit_code == 0, shown.output
    assert "read as DEGIRO Transactions (nl)" in shown.output
    assert "2 new, 0 duplicate, 0 skipped" in shown.output
    done = runner.invoke(app, ["imports", "commit", tx, "--new", "DEGIRO", "--currency", "EUR"])
    assert done.exit_code == 0 and "2 added" in done.output, done.output
    pf = str(fixtures / "portfolio_nl.csv")
    pid = re.search(r"into DEGIRO", done.output)
    assert pid is not None
    held = runner.invoke(
        app,
        ["imports", "preview", pf, "--preset", "degiro_portfolio", "--as-of", "2025-06-10",
         "--new", "Snapshot"],
    )  # fmt: skip
    assert held.exit_code == 0, held.output
    assert "read as DEGIRO Portfolio (nl)" in held.output and "3 new" in held.output
    wrong = runner.invoke(app, ["imports", "preview", pf, "--preset", "nope", "--new", "x"])
    assert wrong.exit_code != 0
