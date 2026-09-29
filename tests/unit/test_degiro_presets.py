"""DEGIRO exports read without a mapping: Transactions, Account statement
and Portfolio, in every language DEGIRO writes.

The files under tests/fixtures/degiro are made up. The ISINs are public
listings, and the amounts, dates and order ids are invented."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import pytest

from stonks.connections.statement_csv import StatementError, read_headers
from stonks.connections.statement_presets import registry
from stonks.connections.statement_presets.base import Listing, NoResolver

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "degiro"

APPLE, ASML, IWDA, ABI = "US0378331005", "NL0010273215", "IE00B4L5Y983", "BE0974293251"


class FakeResolver:
    """Knows Apple on Nasdaq, ASML in Amsterdam and the MSCI World ETF;
    not AB InBev."""

    known = {APPLE: "AAPL.US", ASML: "ASML.AS", IWDA: "IWDA.AS"}

    def __init__(self) -> None:
        self.asked: list[Listing] = []

    def resolve(self, listings: Sequence[Listing]) -> Mapping[Listing, str | None]:
        self.asked.extend(listings)
        return {li: self.known.get(li.isin) for li in listings}


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(name: str, preset_id: str | None = None, resolver=None):
    text = _text(name)
    if preset_id is None:
        found = registry.detect(read_headers(text, sniff=True))
        assert found is not None, name
        preset = found[0]
    else:
        preset = registry.preset(preset_id)
    return preset.parse(text, resolver or FakeResolver())


@pytest.mark.parametrize(
    ("name", "preset_id", "locale"),
    [
        ("transactions_en.csv", "degiro_transactions", "en"),
        ("transactions_nl.csv", "degiro_transactions", "nl"),
        ("transactions_de_old.csv", "degiro_transactions", "de"),
        ("transactions_es.csv", "degiro_transactions", "es"),
        ("transactions_fr.csv", "degiro_transactions", "fr"),
        ("transactions_it.csv", "degiro_transactions", "it"),
        ("transactions_pt.csv", "degiro_transactions", "pt"),
        ("account_en.csv", "degiro_account", "en"),
        ("account_nl.csv", "degiro_account", "nl"),
        ("account_de.csv", "degiro_account", "de"),
        ("account_fr.csv", "degiro_account", "fr"),
        ("account_es.csv", "degiro_account", "es"),
        ("account_it.csv", "degiro_account", "it"),
        ("account_pt.csv", "degiro_account", "pt"),
        ("portfolio_en.csv", "degiro_portfolio", "en"),
        ("portfolio_nl.csv", "degiro_portfolio", "nl"),
        ("portfolio_de.csv", "degiro_portfolio", "de"),
    ],
)
def test_each_export_and_language_is_found_from_its_headers(name, preset_id, locale):
    found = registry.detect(read_headers(_text(name), sniff=True))
    assert found is not None
    preset, got = found
    assert (preset.id, got) == (preset_id, locale)


def test_a_semicolon_file_is_read_too():
    text = _text("transactions_nl.csv").replace(",", ";")
    # a decimal comma inside quotes stays a decimal comma
    text = text.replace('"650;2000"', '"650,2000"')
    found = registry.detect(read_headers(text, sniff=True))
    assert found is not None and found[0].id == "degiro_transactions"


# ---- Transactions ---------------------------------------------------------------------


def test_transactions_in_english_become_trades_in_the_account_currency():
    out = _parse("transactions_en.csv")
    assert out.preset == "degiro_transactions" and out.kind == "activities"
    rows = [r.activity for r in out.rows]
    assert all(a is not None for a in rows)
    sell, abi, fill2, fill1, asml, buy = rows
    # a USD trade on a EUR account: every money field in EUR
    assert buy.kind == "trade" and buy.ticker == "AAPL.US" and buy.raw_symbol == APPLE
    assert buy.quantity == 10 and buy.currency == "EUR"
    assert buy.amount == pytest.approx(-1939.44)
    assert buy.fee == pytest.approx(6.83)  # 2.00 fees + 4.83 AutoFX
    assert buy.price == pytest.approx(193.261)  # 1932.61 EUR / 10
    assert "210.5 USD" in buy.description and "rate 1.0892" in buy.description
    assert buy.trade_date == date(2025, 3, 15)
    # a sale has a negative quantity and brings money in
    assert sell.quantity == -4 and sell.amount == pytest.approx(829.18)
    assert sell.fee == pytest.approx(4.08)
    # a EUR trade keeps its own price
    assert asml.price == pytest.approx(650.20) and asml.fee == pytest.approx(4.90)
    assert asml.ticker == "ASML.AS"
    # two partial fills of one order stay two activities
    assert fill1.provider_activity_id != fill2.provider_activity_id
    assert fill1.quantity == fill2.quantity == 5
    # AB InBev is not in the lake: kept by ISIN, flagged
    assert abi.ticker is None and abi.raw_symbol == ABI
    assert out.unmapped == {ABI: "ANHEUSER-BUSCH INBEV"}
    assert all(a.provider_activity_id.startswith("degiro:tx:") for a in rows)


def test_the_reference_exchange_and_currency_go_to_the_resolver():
    resolver = FakeResolver()
    _parse("transactions_en.csv", resolver=resolver)
    by_isin = {li.isin: li for li in resolver.asked}
    assert by_isin[APPLE] == Listing(APPLE, "US", "USD")
    assert by_isin[ASML] == Listing(ASML, "AS", "EUR")
    assert by_isin[ABI] == Listing(ABI, "BR", "EUR")


def test_ids_are_stable_so_a_file_imported_twice_adds_nothing():
    first = [r.activity.provider_activity_id for r in _parse("transactions_en.csv").rows]
    again = [r.activity.provider_activity_id for r in _parse("transactions_en.csv").rows]
    assert first == again and len(set(first)) == len(first)


def test_dutch_decimal_commas_read_the_same_as_english_points():
    en = {r.activity.raw_symbol: r.activity for r in _parse("transactions_en.csv").rows}
    nl = [r.activity for r in _parse("transactions_nl.csv").rows]
    for a in nl:
        match = en[a.raw_symbol] if a.raw_symbol == ASML else None
        if match is not None:
            assert (a.amount, a.fee, a.price) == pytest.approx(
                (match.amount, match.fee, match.price)
            )
    apple = next(a for a in nl if a.raw_symbol == APPLE)
    assert apple.amount == pytest.approx(-1939.44) and apple.fee == pytest.approx(6.83)
    assert apple.price == pytest.approx(193.261)


def test_the_older_german_layout_with_a_currency_column_after_each_amount():
    out = _parse("transactions_de_old.csv")
    asml, apple = (r.activity for r in out.rows)
    assert asml.amount == pytest.approx(-1955.50) and asml.currency == "EUR"
    assert apple.amount == pytest.approx(-1934.61) and apple.fee == pytest.approx(2.00)
    assert apple.currency == "EUR" and apple.price == pytest.approx(193.261)


@pytest.mark.parametrize("name", ["transactions_es.csv", "transactions_fr.csv",
                                  "transactions_it.csv", "transactions_pt.csv"])  # fmt: skip
def test_the_other_languages_read_the_same_trade(name):
    [row] = _parse(name).rows
    a = row.activity
    assert a is not None and a.ticker == "ASML.AS" and a.quantity == 3
    assert a.amount == pytest.approx(-1955.50) and a.fee == pytest.approx(4.90)
    assert a.price == pytest.approx(650.20) and a.currency == "EUR"


def test_a_bad_line_is_skipped_with_its_reason():
    text = _text("transactions_en.csv").splitlines()
    bad = text[1].replace("02-05-2025", "2025/13/45")
    out = registry.preset("degiro_transactions").parse(
        "\n".join([text[0], bad, text[2]]) + "\n", NoResolver()
    )
    skipped, kept = out.rows
    assert skipped.activity is None and "DEGIRO date" in skipped.skipped
    assert kept.activity is not None and kept.activity.ticker is None  # no resolver


def test_other_files_are_refused_by_a_named_preset():
    with pytest.raises(StatementError, match="not the headers of a DEGIRO Transactions"):
        registry.preset("degiro_transactions").parse("Date,Action\n2025-01-01,BUY\n", NoResolver())
    with pytest.raises(StatementError, match="empty"):
        registry.preset("degiro_account").parse("", NoResolver())


# ---- Account statement ----------------------------------------------------------------


def test_the_english_account_statement_keeps_cash_lines_and_leaves_trades_out():
    out = _parse("account_en.csv")
    kept = [r.activity for r in out.rows if r.activity is not None]
    skipped = {r.line: r.skipped for r in out.rows if r.activity is None}
    by_desc = {a.description.split(" (")[0]: a for a in kept}
    assert by_desc["Processed Flatex Withdrawal"].kind == "withdrawal"
    assert by_desc["Processed Flatex Withdrawal"].amount == -300.0
    assert by_desc["Flatex Interest Income"].kind == "interest"
    assert by_desc["iDEAL Deposit"].kind == "deposit" and by_desc["iDEAL Deposit"].amount == 5000
    assert by_desc["DEGIRO Exchange Connection Fee 2025"].kind == "fee"
    dividend = by_desc["Dividend"]
    assert dividend.kind == "dividend" and dividend.amount == 2.60 and dividend.currency == "USD"
    assert dividend.ticker == "AAPL.US" and dividend.raw_symbol == APPLE
    tax = by_desc["Dividend tax: Dividend Tax"]
    assert tax.kind == "dividend" and tax.amount == -0.39
    # the dividend's currency legs are kept as Other so the cash adds up
    fx = [a for a in kept if a.kind == "other"]
    assert sorted((a.currency, a.amount) for a in fx) == [("EUR", 1.95), ("USD", -2.21)]
    assert all("Currency conversion" in a.description for a in fx)
    assert "rate 1.1333" in next(a for a in fx if a.currency == "EUR").description
    # trade lines, their fees and AutoFX legs, the sweep, the reservation
    # and an unknown line are skipped with a reason
    reasons = list(skipped.values())
    assert sum("part of a trade" in r for r in reasons) == 7
    assert any("inside DEGIRO" in r for r in reasons)
    assert any("reservation" in r for r in reasons)
    assert any("Something DEGIRO added later" in r for r in reasons)
    assert any("7 lines belong to trades" in n for n in out.notes)
    assert len(kept) == 8


def test_cash_from_both_files_adds_up_once():
    """Transactions give the trades in EUR and the account statement the
    rest. Nothing is counted twice, and the USD dividend nets to zero once
    its conversion to EUR is counted."""
    trades = [r.activity for r in _parse("transactions_en.csv").rows]
    cash = [r.activity for r in _parse("account_en.csv").rows if r.activity is not None]
    # only the Apple trades appear in account_en; take those two
    apple = [a for a in trades if a.raw_symbol == APPLE]
    eur = sum(a.amount for a in apple + cash if a.currency == "EUR")
    usd = sum(a.amount for a in cash if a.currency == "USD")
    assert usd == pytest.approx(0.0)
    # 5000 - 1939.44 + 829.18 - 2.50 + 1.95 + 0.42 - 300 (sweep of 100 is internal)
    assert eur == pytest.approx(5000 - 1939.44 + 829.18 - 2.50 + 1.95 + 0.42 - 300)


@pytest.mark.parametrize(
    "name", ["account_nl.csv", "account_de.csv", "account_fr.csv", "account_es.csv",
             "account_it.csv", "account_pt.csv"],
)  # fmt: skip
def test_dividends_tax_and_deposits_in_every_language(name):
    out = _parse(name)
    kept = [r.activity for r in out.rows if r.activity is not None]
    dividends = sorted(a.amount for a in kept if a.kind == "dividend")
    assert dividends == [pytest.approx(-0.29), pytest.approx(1.92)]
    assert all(a.ticker == "ASML.AS" for a in kept if a.kind == "dividend")
    deposits = [a.amount for a in kept if a.kind == "deposit"]
    assert deposits == [pytest.approx(3000.0)]
    assert not any(a.kind == "trade" for a in kept)


def test_french_thousands_spaces_and_a_connection_fee():
    kept = [r.activity for r in _parse("account_fr.csv").rows if r.activity is not None]
    assert [a.amount for a in kept if a.kind == "fee"] == [pytest.approx(-2.50)]
    assert [a.amount for a in kept if a.kind == "deposit"] == [pytest.approx(3000.0)]


def test_dutch_trade_lines_and_the_bank_transfer_are_left_out():
    out = _parse("account_nl.csv")
    reasons = [r.skipped for r in out.rows if r.activity is None]
    assert len(reasons) == 3
    assert sum("part of a trade" in r for r in reasons) == 2
    assert any("inside DEGIRO" in r for r in reasons)


# ---- Portfolio ------------------------------------------------------------------------


def test_the_portfolio_export_gives_holdings_and_cash():
    out = _parse("portfolio_en.csv")
    assert out.kind == "holdings" and out.rows == []
    by_symbol = {h.raw_symbol: h for h in out.holdings}
    apple = by_symbol[APPLE]
    assert apple.ticker == "AAPL.US" and apple.quantity == 6 and apple.price == 229.35
    assert apple.market_value == pytest.approx(1376.10) and apple.currency == "USD"
    cash = by_symbol["CASH:EUR"]
    assert cash.is_cash and cash.quantity == pytest.approx(1543.21) and cash.ticker is None
    assert by_symbol[ABI].ticker is None and ABI in out.unmapped
    assert out.total == 5 and not out.skipped


@pytest.mark.parametrize("name", ["portfolio_nl.csv", "portfolio_de.csv"])
def test_the_portfolio_export_in_other_languages(name):
    out = _parse(name)
    by_symbol = {h.raw_symbol: h for h in out.holdings}
    assert by_symbol[APPLE].quantity == 6 and by_symbol[APPLE].price == pytest.approx(229.35)
    assert by_symbol[APPLE].market_value == pytest.approx(1376.10)
    assert by_symbol["CASH:EUR"].quantity == pytest.approx(1543.21)


def test_holding_ids_are_stable():
    first = [h.row_id for h in _parse("portfolio_en.csv").holdings]
    assert first == [h.row_id for h in _parse("portfolio_en.csv").holdings]
    assert len(set(first)) == len(first)
