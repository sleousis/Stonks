"""CSV statement imports and the demo portfolio through the REST API
(roadmap 23.17)."""

from __future__ import annotations

CSV = """Date,Action,Symbol,Quantity,Price,Amount,Commission,Currency
2026-01-02,DEPOSIT,,,,10000,,USD
2026-01-05,BUY,AAPL,10,190.00,-1901.00,1.00,USD
2026-02-12,DIVIDEND,AAPL,,,2.50,,USD
2026-03-01,SELL,AAPL,4,200,799,1,USD
2026-03-03,TRANSFER,,,,1,,USD
"""
MAPPING = {
    "date": "Date",
    "type": "Action",
    "symbol": "Symbol",
    "quantity": "Quantity",
    "price": "Price",
    "amount": "Amount",
    "fee": "Commission",
    "currency": "Currency",
    "types": {"BUY": "trade", "SELL": "trade", "DIVIDEND": "dividend", "DEPOSIT": "deposit"},
    "sell_values": ["SELL"],
}


def _body(**extra):
    return {"content": CSV, "mapping": MAPPING, "filename": "jan.csv", **extra}


def test_preview_import_dedupe_and_undo(client, people):
    alice = people["alice"]["headers"]
    preview = client.post(
        "/api/statement-imports/preview", json=_body(new_portfolio="Old broker"), headers=alice
    )
    assert preview.status_code == 200, preview.text
    p = preview.json()
    assert (p["total"], p["new"], p["duplicate"], p["skipped"]) == (5, 4, 0, 1)
    assert p["rows"][4]["status"] == "skipped" and "TRANSFER" in p["rows"][4]["reason"]
    assert p["rows"][1]["ticker"] == "AAPL.US" and p["guessed"] is False
    # nothing was written by the preview
    assert client.get("/api/statement-imports", headers=alice).json()["total"] == 0

    made = client.post(
        "/api/statement-imports", json=_body(new_portfolio="Old broker"), headers=alice
    )
    assert made.status_code == 201, made.text
    imp = made.json()
    assert imp["rows_added"] == 4 and imp["rows_skipped"] == 1
    pid = imp["portfolio_id"]

    # the holdings the trades add up to, like a synced broker account
    insight = client.get("/api/insights", params={"portfolio_id": pid}, headers=alice)
    assert insight.status_code == 200, insight.text
    view = insight.json()
    assert view["source"] == "sync" and view["cash"] == 8900.5  # 10000 - 1901 + 2.5 + 799
    flows = client.get(f"/api/portfolios/{pid}/cash-flows", headers=alice).json()
    assert [f["amount"] for f in flows["items"]] == [10000.0]

    # the same file again: all duplicates, nothing new
    again = client.post(
        "/api/statement-imports/preview", json=_body(portfolio_id=pid), headers=alice
    ).json()
    assert again["duplicate"] == 4 and again["new"] == 0
    refused = client.post("/api/statement-imports", json=_body(portfolio_id=pid), headers=alice)
    assert refused.status_code == 409

    undone = client.post(f"/api/statement-imports/{imp['id']}/undo", headers=alice)
    assert undone.status_code == 200 and undone.json()["undone_at"]
    assert client.post(f"/api/statement-imports/{imp['id']}/undo", headers=alice).status_code == 409
    flows = client.get(f"/api/portfolios/{pid}/cash-flows", headers=alice).json()
    assert flows["items"] == []
    # after an undo the rows can come in again
    redo = client.post("/api/statement-imports", json=_body(portfolio_id=pid), headers=alice)
    assert redo.status_code == 201 and redo.json()["rows_added"] == 4


def test_imports_are_private_and_need_a_trader(client, people):
    alice, bob, vic = (people[n]["headers"] for n in ("alice", "bob", "vic"))
    imp = client.post(
        "/api/statement-imports", json=_body(new_portfolio="Mine"), headers=alice
    ).json()
    assert client.post(f"/api/statement-imports/{imp['id']}/undo", headers=bob).status_code == 404
    assert client.get("/api/statement-imports", headers=bob).json()["total"] == 0
    other = client.post(
        "/api/statement-imports/preview", json=_body(portfolio_id=imp["portfolio_id"]), headers=bob
    )
    assert other.status_code == 404
    assert client.get("/api/statement-imports", headers=vic).status_code == 200
    viewer = client.post(
        "/api/statement-imports/preview", json=_body(new_portfolio="V"), headers=vic
    )
    assert viewer.status_code == 403


def test_bad_statements_are_refused(client, people):
    alice = people["alice"]["headers"]
    both = client.post(
        "/api/statement-imports/preview",
        json=_body(new_portfolio="x", portfolio_id="pf_1"),
        headers=alice,
    )
    assert both.status_code == 422
    bad_column = client.post(
        "/api/statement-imports/preview",
        json=_body(new_portfolio="x", mapping={"date": "When", "kind": "trade"}),
        headers=alice,
    )
    assert bad_column.status_code == 422 and "When" in bad_column.text
    guessed = client.post(
        "/api/statement-imports/preview", json={"content": CSV, "new_portfolio": "x"}, headers=alice
    )
    assert guessed.status_code == 200 and guessed.json()["guessed"] is True


def test_the_demo_portfolio_is_labelled_private_and_removable(client, people):
    alice, bob, vic = (people[n]["headers"] for n in ("alice", "bob", "vic"))
    assert client.get("/api/demo", headers=alice).json()["exists"] is False
    opened = client.post("/api/demo", headers=vic)
    assert opened.status_code == 200, opened.text
    demo = opened.json()
    assert demo["exists"] and demo["label"] == "Sample data"
    assert all(p["ticker"].endswith(".DEMO") for p in demo["positions"])
    assert len(demo["curve"]) == 250
    assert client.post("/api/demo", headers=vic).json()["total_value"] == demo["total_value"]
    assert client.get("/api/demo", headers=bob).json()["exists"] is False
    # it never becomes a real portfolio
    assert all(
        "Demo" not in p["name"] for p in client.get("/api/portfolios", headers=vic).json()["items"]
    )
    assert client.delete("/api/demo", headers=vic).status_code == 204
    assert client.get("/api/demo", headers=vic).json()["exists"] is False
