"""Tax routes (roadmap 20.5) through the API: settings, specific-lot picks
and the yearly CSV exports, each scoped to the caller's own portfolios."""

from __future__ import annotations

import csv
import io

import pandas as pd
import pytest

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture
def book(settings, people) -> dict:
    """Alice's portfolio with two buys and a loss sale of UP.US in 2025,
    a repurchase inside 30 days, and a 2025 dividend."""
    alice = people["alice"]
    with SqliteState(settings.state.path) as state:
        pid = (
            PortfolioRepository(state)
            .create(Scope(user_id=alice["id"], role=Role.TRADER), name="TaxBook")
            .id
        )
        fills = [
            ("o1", "buy", 10, 100.0, "2025-01-02T15:00:00+00:00"),
            ("o2", "buy", 10, 120.0, "2025-02-03T15:00:00+00:00"),
            ("o3", "sell", 12, 90.0, "2025-03-03T15:00:00+00:00"),
            ("o4", "buy", 2, 95.0, "2025-03-20T15:00:00+00:00"),
        ]
        ids = {}
        for cid, side, qty, price, at in fills:
            state.execute(
                "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
                " created_at, updated_at, portfolio_id) VALUES (?, 'UP.US', ?, ?, 'market',"
                " 'filled', ?, ?, ?)",
                [cid, side, qty, at, at, pid],
            )
            ids[cid] = state.execute(
                "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
                " portfolio_id) VALUES (?, 'UP.US', ?, ?, 0, ?, ?)",
                [cid, qty, price, at, pid],
            ).lastrowid
        state.execute(
            "INSERT INTO corporate_action_ledger (portfolio_id, ticker, ex_date, kind, value,"
            " quantity_before, quantity_after, cash_delta, applied_at) VALUES"
            " (?, 'UP.US', '2025-02-10', 'dividend', 1.0, 20, 20, 17.0, '2025-02-10')",
            [pid],
        )
    return {"pid": pid, "fills": ids}


def test_settings_default_update_and_audit(client, people, book, settings):
    alice = people["alice"]["headers"]
    q = {"portfolio_id": book["pid"]}
    got = client.get("/api/tax/settings", params=q, headers=alice)
    assert got.status_code == 200, got.text
    assert got.json() | {"updated_at": None} == {
        "portfolio_id": book["pid"],
        "base_currency": "USD",
        "jurisdiction": "us",
        "lot_method": "fifo",
        "wash_sales": True,
        "updated_at": None,
        "locked": False,
    }
    put = client.put(
        "/api/tax/settings",
        params=q,
        json={"base_currency": "EUR", "jurisdiction": "eu"},
        headers=alice,
    )
    assert put.status_code == 200, put.text
    assert put.json()["base_currency"] == "EUR" and put.json()["jurisdiction"] == "eu"
    assert put.json()["lot_method"] == "fifo"
    bad = client.put("/api/tax/settings", params=q, json={"base_currency": "eur"}, headers=alice)
    assert bad.status_code == 422
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT actor, action FROM audit_log WHERE action = 'tax_settings.update'")
    assert [r["actor"] for r in rows] == [f"user:{people['alice']['id']}"]


def test_live_account_facts_are_locked_while_real_money_trades(client, people, book, settings):
    """The jurisdiction and base currency are the account profile's too, so
    they are locked while the portfolio trades real money."""
    from stonks.production.live.stages import change_stage

    alice = people["alice"]["headers"]
    q = {"portfolio_id": book["pid"]}
    with SqliteState(settings.state.path) as state:
        for stage in ("broker_paper", "live_small"):
            change_stage(
                state, book["pid"], stage, actor="u", reason="r",
                gate_report={"target": stage, "passed": True},
            )  # fmt: skip
    view = client.get("/api/tax/settings", params=q, headers=alice)
    assert view.json()["locked"] is True
    for body in ({"jurisdiction": "eu"}, {"base_currency": "EUR"}):
        locked = client.put("/api/tax/settings", params=q, json=body, headers=alice)
        assert locked.status_code == 409, locked.text
    fine = client.put("/api/tax/settings", params=q, json={"lot_method": "specific"}, headers=alice)
    assert fine.status_code == 200, fine.text


def test_other_people_get_404_and_viewers_cannot_write(client, people, book):
    q = {"portfolio_id": book["pid"]}
    bob, vic, ada = (people[n]["headers"] for n in ("bob", "vic", "ada"))
    for headers in (bob, ada):
        assert client.get("/api/tax/settings", params=q, headers=headers).status_code == 404
        assert (
            client.get(
                "/api/tax/exports/gains", params=q | {"year": 2025}, headers=headers
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/api/tax/settings", params=q, json={"wash_sales": False}, headers=headers
            ).status_code
            == 404
        )
    assert client.put("/api/tax/settings", params=q, json={}, headers=vic).status_code == 403
    assert (
        client.put(
            "/api/tax/lots/picks", params=q, json={"sell_fill_id": 1}, headers=vic
        ).status_code
        == 403
    )


def test_gains_csv_with_wash_sale(client, people, book):
    alice = people["alice"]["headers"]
    resp = client.get(
        "/api/tax/exports/gains",
        params={"portfolio_id": book["pid"], "year": 2025},
        headers=alice,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    rows = _rows(resp.text)
    # FIFO: 10 from the first buy (loss 100), 2 from the second (loss 60)
    assert [(r["quantity"], r["cost_basis"]) for r in rows] == [("10", "1000.00"), ("2", "240.00")]
    # the 2-share repurchase disallows 2/10 of the first loss
    assert rows[0]["wash_sale_disallowed"] == "20.00"
    assert rows[0]["gain"] == "-80.00"
    empty = client.get(
        "/api/tax/exports/gains",
        params={"portfolio_id": book["pid"], "year": 2024},
        headers=alice,
    )
    assert _rows(empty.text) == []


def test_specific_lot_picks(client, people, book):
    alice = people["alice"]["headers"]
    q = {"portfolio_id": book["pid"]}
    f = book["fills"]
    client.put("/api/tax/settings", params=q, json={"lot_method": "specific"}, headers=alice)
    bad = client.put(
        "/api/tax/lots/picks",
        params=q,
        json={"sell_fill_id": f["o3"], "picks": [{"buy_fill_id": f["o4"], "quantity": 1}]},
        headers=alice,
    )
    assert bad.status_code == 422  # bought after the sell
    too_much = client.put(
        "/api/tax/lots/picks",
        params=q,
        json={"sell_fill_id": f["o3"], "picks": [{"buy_fill_id": f["o2"], "quantity": 11}]},
        headers=alice,
    )
    assert too_much.status_code == 422
    ok = client.put(
        "/api/tax/lots/picks",
        params=q,
        json={"sell_fill_id": f["o3"], "picks": [{"buy_fill_id": f["o2"], "quantity": 10}]},
        headers=alice,
    )
    assert ok.status_code == 200, ok.text
    assert [p["buy_fill_id"] for p in ok.json()] == [f["o2"]]
    listed = client.get("/api/tax/lots/picks", params=q, headers=alice).json()
    assert len(listed["items"]) == 1 and listed["total"] == 1
    rows = _rows(
        client.get("/api/tax/exports/gains", params=q | {"year": 2025}, headers=alice).text
    )
    assert [(r["open_fill_id"], r["quantity"]) for r in rows] == [
        (str(f["o2"]), "10"),
        (str(f["o1"]), "2"),
    ]
    bob = people["bob"]["headers"]
    assert client.get("/api/tax/lots/picks", params=q, headers=bob).status_code == 404


def test_dividends_csv(client, people, book):
    rows = _rows(
        client.get(
            "/api/tax/exports/dividends",
            params={"portfolio_id": book["pid"], "year": 2025},
            headers=people["alice"]["headers"],
        ).text
    )
    assert len(rows) == 1
    assert (rows[0]["gross"], rows[0]["withholding"], rows[0]["net"]) == (
        "20.00",
        "3.00",
        "17.00",
    )


def test_open_lots_csv(client, people, book):
    """FIFO leaves 8 of the second buy and the 2-share repurchase, which
    carries the disallowed wash sale loss (roadmap 13.12)."""
    alice = people["alice"]["headers"]
    q = {"portfolio_id": book["pid"]}
    f = book["fills"]
    early = client.get("/api/tax/exports/lots", params=q | {"as_of": "2025-09-30"}, headers=alice)
    assert early.status_code == 200, early.text
    assert early.headers["content-type"].startswith("text/csv")
    assert "tax-lots-" in early.headers["content-disposition"]
    rows = _rows(early.text)
    assert [(r["open_fill_id"], r["quantity"], r["cost_basis"]) for r in rows] == [
        (str(f["o2"]), "8", "960.00"),
        (str(f["o4"]), "2", "210.00"),
    ]
    assert rows[1]["wash_sale_adjustment"] == "20.00"
    assert all(r["holding_period"] == "short" and r["price"] == "" for r in rows)
    assert rows[0]["long_term_on"] == "2026-02-04"

    late = _rows(
        client.get("/api/tax/exports/lots", params=q | {"as_of": "2026-04-01"}, headers=alice).text
    )
    assert late[0]["holding_period"] == "long"
    assert late[0]["price"] == "200.000000"
    assert late[0]["unrealized_gain"] == "640.00"
    before = _rows(
        client.get("/api/tax/exports/lots", params=q | {"as_of": "2025-01-31"}, headers=alice).text
    )
    assert [r["open_fill_id"] for r in before] == [str(f["o1"])]
    bob = people["bob"]["headers"]
    assert client.get("/api/tax/exports/lots", params=q, headers=bob).status_code == 404


def test_fx_rate_read(client, people, settings):
    lake = DuckDBLake(settings.lake.path)
    lake.upsert_fx_rates(
        pd.DataFrame(
            [
                {
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "observation_date": pd.Timestamp("2025-01-02").date(),
                    "rate": 1.25,
                    "source": "fake",
                }
            ]
        )
    )
    lake.close()
    got = client.get(
        "/api/fx/rate",
        params={"base": "usd", "quote": "EUR", "day": "2025-03-01"},
        headers=people["vic"]["headers"],
    )
    assert got.status_code == 200, got.text
    assert got.json()["rate"] == pytest.approx(0.8)
    none = client.get(
        "/api/fx/rate", params={"base": "CHF", "quote": "USD"}, headers=people["vic"]["headers"]
    )
    assert none.json()["rate"] is None


def test_a_commission_in_another_currency_is_converted(client, people, book, settings):
    from stonks.ingest.pipeline import _rows_to_df
    from stonks.ingest.schemas import TickerProfile

    lake = DuckDBLake(settings.lake.path)
    lake.migrate()
    lake.upsert_instrument_profile(
        _rows_to_df([TickerProfile(id="EU.XETRA", name="Eu", asset_class="equity", currency="EUR")])
    )
    lake.upsert_fx_rates(
        pd.DataFrame(
            [
                {
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "observation_date": pd.Timestamp("2025-01-02").date(),
                    "rate": 1.25,
                    "source": "fake",
                }
            ]
        )
    )
    lake.close()
    pid = book["pid"]
    with SqliteState(settings.state.path) as state:
        for cid, side, price, fee, fee_ccy, at in [
            ("e1", "buy", 100.0, 1.25, "USD", "2025-04-01T09:00:00+00:00"),
            ("e2", "sell", 110.0, 0.0, None, "2025-05-02T09:00:00+00:00"),
        ]:
            state.execute(
                "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
                " created_at, updated_at, portfolio_id) VALUES (?, 'EU.XETRA', ?, 10, 'market',"
                " 'filled', ?, ?, ?)",
                [cid, side, at, at, pid],
            )
            state.execute(
                "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, fee_currency,"
                " filled_at, portfolio_id) VALUES (?, 'EU.XETRA', 10, ?, ?, ?, ?, ?)",
                [cid, price, fee, fee_ccy, at, pid],
            )
    rows = _rows(
        client.get(
            "/api/tax/exports/gains",
            params={"portfolio_id": pid, "year": 2025},
            headers=people["alice"]["headers"],
        ).text
    )
    [row] = [r for r in rows if r["ticker"] == "EU.XETRA"]
    # the 1.25 USD commission is 1.00 EUR at 1.25 USD per EUR
    assert (row["currency"], row["cost_basis"]) == ("EUR", "1001.00")
