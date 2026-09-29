"""DEGIRO exports through the REST API: found from their headers, mapped to
tickers by ISIN, deduplicated, undone, and read by insights, the behaviour
report and the tax exports. The files are made up (tests/fixtures/degiro)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from stonks.store.lake import DuckDBLake

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "degiro"
APPLE, ABI = "US0378331005", "BE0974293251"


def _file(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture
def isins(settings):
    """The lake knows Apple twice (Nasdaq and Xetra), ASML and the MSCI
    World ETF by ISIN; not AB InBev."""
    lake = DuckDBLake(settings.lake.path)
    try:
        lake.con.execute(
            "INSERT INTO instruments (id, asset_class, exchange, currency, isin) VALUES"
            " ('AAPL.US', 'equity', 'NASDAQ', 'USD', 'US0378331005'),"
            " ('APC.XETRA', 'equity', 'XETRA', 'EUR', 'US0378331005'),"
            " ('ASML.AS', 'equity', 'AS', 'EUR', 'NL0010273215'),"
            " ('IWDA.AS', 'etf', 'AS', 'EUR', 'IE00B4L5Y983')"
            " ON CONFLICT DO NOTHING"
        )
    finally:
        lake.close()


def _import(client, headers, name, **target):
    body = {"content": _file(name), "filename": name, **target}
    return client.post("/api/statement-imports", json=body, headers=headers)


def test_presets_are_listed(client, people):
    got = client.get("/api/statement-imports/presets", headers=people["vic"]["headers"])
    assert got.status_code == 200, got.text
    by_id = {p["id"]: p for p in got.json()}
    assert by_id["degiro_transactions"]["broker"] == "DEGIRO"
    assert by_id["degiro_portfolio"]["kind"] == "holdings"
    assert "Inbox" in by_id["degiro_account"]["how_to_export"]


def test_a_degiro_account_comes_in_from_its_exports(client, people, isins):
    alice = people["alice"]["headers"]
    preview = client.post(
        "/api/statement-imports/preview",
        json={"content": _file("transactions_en.csv"), "new_portfolio": "DEGIRO",
              "currency": "EUR"},
        headers=alice,
    )  # fmt: skip
    assert preview.status_code == 200, preview.text
    p = preview.json()
    assert (p["preset"], p["locale"], p["kind"], p["mapping"]) == (
        "degiro_transactions", "en", "activities", None,
    )  # fmt: skip
    assert p["preset_label"] == "DEGIRO Transactions" and p["guessed"] is False
    assert (p["new"], p["duplicate"], p["skipped"]) == (6, 0, 0)
    assert p["unmapped"] == [f"{ABI} (ANHEUSER-BUSCH INBEV)"]
    assert {r["ticker"] for r in p["rows"]} == {"AAPL.US", "ASML.AS", "IWDA.AS", None}
    assert p["notes"]

    trades = _import(client, alice, "transactions_en.csv", new_portfolio="DEGIRO", currency="EUR")
    assert trades.status_code == 201, trades.text
    t = trades.json()
    assert (t["rows_added"], t["preset"], t["kind"]) == (6, "degiro_transactions", "activities")
    pid = t["portfolio_id"]

    cash = _import(client, alice, "account_en.csv", portfolio_id=pid)
    assert cash.status_code == 201, cash.text
    assert (cash.json()["rows_added"], cash.json()["rows_skipped"]) == (8, 10)

    # the same files again add nothing
    assert _import(client, alice, "transactions_en.csv", portfolio_id=pid).status_code == 409
    again = client.post(
        "/api/statement-imports/preview",
        json={"content": _file("account_en.csv"), "portfolio_id": pid},
        headers=alice,
    ).json()
    assert (again["new"], again["duplicate"]) == (0, 8)

    # holdings from the activities, the unmapped one by its ISIN
    view = client.get("/api/insights", params={"portfolio_id": pid}, headers=alice).json()
    assert view["source"] == "sync"
    assert view["uncovered"] == [ABI]
    deposits = client.get(f"/api/portfolios/{pid}/cash-flows", headers=alice).json()["items"]
    assert sorted(f["amount"] for f in deposits) == [300.0, 5000.0]

    # the behaviour report reads the imported round trip
    behaviour = client.get("/api/insights/behaviour", params={"portfolio_id": pid}, headers=alice)
    assert behaviour.status_code == 200, behaviour.text

    # tax: the Apple sale in EUR, with the AutoFX cost in the basis
    gains = _rows(
        client.get(
            "/api/tax/exports/gains", params={"portfolio_id": pid, "year": 2025}, headers=alice
        ).text
    )
    [apple] = [g for g in gains if g["ticker"] == "AAPL.US"]
    assert apple["quantity"] == "4" and apple["currency"] == "EUR"
    # cost 4/10 of 1932.61 + 6.83; proceeds 833.26 less 4.08 of fees
    assert float(apple["gain"]) == pytest.approx(833.26 - 4.08 - 0.4 * (1932.61 + 6.83), abs=0.01)
    dividends = _rows(
        client.get(
            "/api/tax/exports/dividends", params={"portfolio_id": pid, "year": 2025}, headers=alice
        ).text
    )
    [div] = dividends
    assert (div["ticker"], div["currency"], div["quantity"]) == ("AAPL.US", "USD", "6")
    assert (div["gross"], div["withholding"], div["net"]) == ("2.60", "0.39", "2.21")

    # undo the cash import: its dividends and deposits go
    undone = client.post(f"/api/statement-imports/{cash.json()['id']}/undo", headers=alice)
    assert undone.status_code == 200 and undone.json()["undone_at"]
    assert client.get(f"/api/portfolios/{pid}/cash-flows", headers=alice).json()["items"] == []
    after = client.get(
        "/api/tax/exports/dividends", params={"portfolio_id": pid, "year": 2025}, headers=alice
    ).text
    assert _rows(after) == []


def test_a_portfolio_export_sets_the_holdings_on_its_day(client, people, isins):
    alice = people["alice"]["headers"]
    t = _import(client, alice, "transactions_en.csv", new_portfolio="DEGIRO", currency="EUR")
    pid = t.json()["portfolio_id"]
    body = {"content": _file("portfolio_en.csv"), "portfolio_id": pid, "as_of": "2025-06-10"}
    preview = client.post("/api/statement-imports/preview", json=body, headers=alice).json()
    assert (preview["preset"], preview["kind"], preview["as_of"]) == (
        "degiro_portfolio", "holdings", "2025-06-10",
    )  # fmt: skip
    assert (preview["new"], preview["duplicate"], preview["skipped"]) == (5, 0, 0)
    assert {r["kind"] for r in preview["rows"]} == {"holding", "cash"}

    made = client.post("/api/statement-imports", json=body, headers=alice)
    assert made.status_code == 201, made.text
    m = made.json()
    assert (m["kind"], m["as_of"], m["rows_added"]) == ("holdings", "2025-06-10", 5)
    view = client.get("/api/insights", params={"portfolio_id": pid}, headers=alice).json()
    assert view["cash"] == pytest.approx(1543.21)
    held = {s["key"] for s in view["allocation"]["ticker"]}
    assert {"AAPL.US", "ASML.AS", "IWDA.AS", ABI, "cash"} <= held

    # the same export for the same day is a duplicate; undo brings the
    # holdings back to what the trades add up to
    assert client.post("/api/statement-imports", json=body, headers=alice).status_code == 409
    client.post(f"/api/statement-imports/{m['id']}/undo", headers=alice)
    back = client.get("/api/insights", params={"portfolio_id": pid}, headers=alice).json()
    assert back["cash"] == pytest.approx(-1939.44 - 1955.50 - 476.50 - 475.60 - 114.70 + 829.18)


def test_presets_are_named_or_refused(client, people):
    alice = people["alice"]["headers"]
    named = client.post(
        "/api/statement-imports/preview",
        json={"content": _file("account_nl.csv"), "new_portfolio": "x",
              "preset": "degiro_account"},
        headers=alice,
    )  # fmt: skip
    assert named.status_code == 200 and named.json()["locale"] == "nl"
    wrong = client.post(
        "/api/statement-imports/preview",
        json={"content": _file("account_nl.csv"), "new_portfolio": "x",
              "preset": "degiro_portfolio"},
        headers=alice,
    )  # fmt: skip
    assert wrong.status_code == 422 and "Portfolio" in wrong.text
    unknown = client.post(
        "/api/statement-imports/preview",
        json={"content": "Date,Action\n2025-01-01,BUY\n", "new_portfolio": "x", "preset": "auto"},
        headers=alice,
    )
    assert unknown.status_code == 422 and "map the columns" in unknown.text
    both = client.post(
        "/api/statement-imports/preview",
        json={"content": "Date\n", "new_portfolio": "x", "preset": "degiro_account",
              "mapping": {"date": "Date", "kind": "trade"}},
        headers=alice,
    )  # fmt: skip
    assert both.status_code == 422
