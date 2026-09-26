"""Unit tests for the execution order-id helper."""

from __future__ import annotations

from datetime import date

from stonks.execution.orders import make_client_id

D1 = date(2026, 3, 20)
D2 = date(2026, 3, 23)


def test_client_id_is_deterministic_for_same_inputs():
    a = make_client_id(as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy")
    b = make_client_id(as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy")
    assert a == b


def test_client_id_differs_by_side():
    buy = make_client_id(as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy")
    sell = make_client_id(as_of=D1, strategy_id="s1", ticker="AAPL.US", side="sell")
    assert buy != sell


def test_client_id_differs_by_as_of_date():
    a = make_client_id(as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy")
    b = make_client_id(as_of=D2, strategy_id="s1", ticker="AAPL.US", side="buy")
    assert a != b


def test_client_id_differs_by_strategy():
    a = make_client_id(as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy")
    b = make_client_id(as_of=D1, strategy_id="s2", ticker="AAPL.US", side="buy")
    assert a != b


def test_client_id_shape_contains_date_prefix():
    cid = make_client_id(as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy")
    assert cid.startswith("2026-03-20:")
    assert "AAPL.US" in cid
    assert "buy" in cid


# ---- portfolio ids (surviving mutants found by TT-04) -------------------------


def test_a_non_default_portfolio_puts_its_id_after_the_date():
    cid = make_client_id(
        as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy", portfolio_id="pf_abc"
    )
    assert cid == "2026-03-20:pf_abc:s1:AAPL.US:buy"


def test_the_default_portfolio_and_none_keep_the_plain_format():
    from stonks.accounts.models import DEFAULT_PORTFOLIO_ID

    plain = "2026-03-20:s1:AAPL.US:buy"
    for portfolio_id in (None, DEFAULT_PORTFOLIO_ID):
        cid = make_client_id(
            as_of=D1, strategy_id="s1", ticker="AAPL.US", side="buy", portfolio_id=portfolio_id
        )
        assert cid == plain
