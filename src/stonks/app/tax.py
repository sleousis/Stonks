"""TaxService (roadmap 20.5): a portfolio's tax settings, specific-lot
picks, yearly CSV exports of realized gains and dividends, and the open
lots on a day (roadmap 13.12).

Callers resolve ``portfolio_id`` to one the caller owns first
(``PortfolioService.resolve`` in the API, ``owned_portfolio`` in the CLI).
Writes take the caller (a :class:`Principal` from the API, or a bare
:class:`Scope` from the CLI) and write an ``audit_log`` row."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from stonks.accounts import Role, Scope
from stonks.accounts.audit import AuditLog
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, ValidationError
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.fx import FxRates, load_fx_rates
from stonks.store.state import SqliteState
from stonks.tax import (
    DIVIDEND_COLUMNS,
    GAINS_COLUMNS,
    OPEN_LOT_COLUMNS,
    DividendEvent,
    OpenLot,
    TaxFill,
    TaxSettings,
    TaxSplit,
    dividend_rows,
    gains_rows,
    open_lot_rows,
    open_lots,
    realized_disposals,
    to_csv,
)
from stonks.tax.preview import preview_trade, year_tax

Jurisdiction = Literal["us", "eu", "uk"]
LotMethod = Literal["fifo", "specific"]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Who = Principal | Scope

_EPS = 1e-9


class TaxSettingsView(BaseModel):
    portfolio_id: str
    base_currency: str = Field(description="Reporting currency of values, P&L and exports.")
    jurisdiction: Jurisdiction = Field(description="us applies wash sales when switched on.")
    lot_method: LotMethod = Field(
        description="fifo, or specific: your picks first, then FIFO for the rest."
    )
    wash_sales: bool = Field(description="US wash sale adjustment (us jurisdiction only).")
    updated_at: datetime | None = None
    locked: bool = Field(
        default=False,
        description=(
            "True while the portfolio trades real money: the base currency and jurisdiction"
            " are the live account profile's too and cannot change."
        ),
    )


class TaxSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_currency: Currency | None = None
    jurisdiction: Jurisdiction | None = None
    lot_method: LotMethod | None = None
    wash_sales: bool | None = None


class LotPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buy_fill_id: int
    quantity: float = Field(gt=0)


class LotPicksUpdate(BaseModel):
    """The lots one sell fill closes. An empty list clears the picks."""

    model_config = ConfigDict(extra="forbid")

    sell_fill_id: int
    picks: list[LotPick] = Field(default_factory=list, max_length=500)


class LotPickView(BaseModel):
    sell_fill_id: int
    buy_fill_id: int
    ticker: str
    quantity: float


class TaxRatesView(BaseModel):
    short_term: float = Field(description="Rate on gains held one year or less.")
    long_term: float = Field(description="Rate on gains held more than one year.")


class PreviewLotView(BaseModel):
    open_fill_id: int = Field(description="The fill that opened the lot.")
    kind: Literal["long", "short"]
    quantity: float
    acquired: date
    holding_period: Literal["short", "long"]
    cost_basis: float
    proceeds: float
    gain: float
    wash_sale_disallowed: float


class TaxPreviewView(BaseModel):
    """What a trade would realise now, before it is placed. An estimate at
    your configured rates, not tax advice."""

    portfolio_id: str
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float = Field(description="The price used: yours, else the latest close.")
    currency: str = Field(description="The trade's currency; every amount is in it.")
    jurisdiction: Jurisdiction
    lot_method: LotMethod
    lots: list[PreviewLotView] = Field(description="The lots the trade closes, in order.")
    proceeds: float
    realized_gain: float
    short_term_gain: float
    long_term_gain: float
    wash_sale_disallowed: float
    estimated_tax: float = Field(description="Tax on this trade's own gains (0 on a loss).")
    after_tax_proceeds: float
    year_tax_change: float = Field(
        description="How much the trade changes this year's estimated tax (negative lowers it)."
    )
    wash_sale_warning: str | None = None
    rates: TaxRatesView


class TaxYearView(BaseModel):
    """Gains realised this year and the estimated tax on them, in the base
    currency. Short and long term losses offset gains first."""

    portfolio_id: str
    year: int
    base_currency: str
    jurisdiction: Jurisdiction
    short_term_gain: float
    long_term_gain: float
    wash_sale_disallowed: float
    estimated_tax: float
    disposals: int
    unconverted: int = Field(description="Disposals left out for want of an FX rate.")
    rates: TaxRatesView


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _actor(who: Who) -> str:
    return who.actor


def _check_write(who: Who) -> None:
    if isinstance(who, Principal):
        require(who, Permission.PORTFOLIO_MANAGE)
    elif not (who.is_service or Role(who.role).can_trade):
        # the shell acting as a person (--user) gets that person's rights
        raise PermissionDenied("changing tax settings needs a role that can trade")


class TaxService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    # ---- settings ------------------------------------------------------------------

    def settings(self, portfolio_id: str) -> TaxSettingsView:
        with self._ctx.state() as state:
            return _settings_view(state, portfolio_id)

    def update_settings(
        self, who: Who, portfolio_id: str, body: TaxSettingsUpdate, *, ip: str | None = None
    ) -> TaxSettingsView:
        _check_write(who)
        with self._ctx.state() as state, state.transaction():
            current = _settings_view(state, portfolio_id)
            merged = current.model_copy(
                update={k: v for k, v in body.model_dump().items() if v is not None}
            )
            _refuse_locked_change(state, portfolio_id, current, merged)
            now = _now()
            state.execute(
                "INSERT INTO portfolio_tax_settings (portfolio_id, jurisdiction, lot_method,"
                " wash_sales, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (portfolio_id) DO UPDATE SET jurisdiction = excluded.jurisdiction,"
                " lot_method = excluded.lot_method, wash_sales = excluded.wash_sales,"
                " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                [
                    portfolio_id,
                    merged.jurisdiction,
                    merged.lot_method,
                    int(merged.wash_sales),
                    now,
                    _actor(who),
                ],
            )
            if merged.base_currency != current.base_currency:
                state.execute(
                    "UPDATE portfolios SET base_currency = ? WHERE id = ?",
                    [merged.base_currency, portfolio_id],
                )
            AuditLog(state).record(
                _actor(who),
                "tax_settings.update",
                "portfolio",
                portfolio_id,
                portfolio_id=portfolio_id,
                details=body.model_dump(exclude_none=True),
                ip=ip,
            )
            return _settings_view(state, portfolio_id)

    # ---- specific lots ---------------------------------------------------------------

    def picks(self, portfolio_id: str, sell_fill_id: int | None = None) -> list[LotPickView]:
        sql = (
            "SELECT p.sell_fill_id, p.buy_fill_id, p.quantity, f.ticker FROM tax_lot_picks p"
            " JOIN fills f ON f.id = p.sell_fill_id WHERE p.portfolio_id = ?"
        )
        params: list[object] = [portfolio_id]
        if sell_fill_id is not None:
            sql += " AND p.sell_fill_id = ?"
            params.append(sell_fill_id)
        with self._ctx.state() as state:
            rows = state.sql(sql + " ORDER BY p.sell_fill_id, p.buy_fill_id", params)
        return [
            LotPickView(
                sell_fill_id=r["sell_fill_id"],
                buy_fill_id=r["buy_fill_id"],
                ticker=r["ticker"],
                quantity=float(r["quantity"]),
            )
            for r in rows
        ]

    def set_picks(
        self, who: Who, portfolio_id: str, body: LotPicksUpdate, *, ip: str | None = None
    ) -> list[LotPickView]:
        """Replace the lots one sell fill closes. Every fill must be this
        portfolio's; the buys must be the same ticker, filled before the
        sell, and the quantities fit the sell and each buy."""
        _check_write(who)
        with self._ctx.state() as state, state.transaction():
            sell = _fill(state, portfolio_id, body.sell_fill_id)
            if sell is None or sell["side"] != "sell":
                raise ValidationError(f"fill {body.sell_fill_id} is not a sell of this portfolio")
            total = 0.0
            seen: set[int] = set()
            for pick in body.picks:
                if pick.buy_fill_id in seen:
                    raise ValidationError(f"buy fill {pick.buy_fill_id} is picked twice")
                seen.add(pick.buy_fill_id)
                buy = _fill(state, portfolio_id, pick.buy_fill_id)
                if buy is None or buy["side"] != "buy":
                    raise ValidationError(f"fill {pick.buy_fill_id} is not a buy of this portfolio")
                if buy["ticker"] != sell["ticker"]:
                    raise ValidationError(f"fill {pick.buy_fill_id} is another ticker")
                if (buy["filled_at"], buy["id"]) >= (sell["filled_at"], sell["id"]):
                    raise ValidationError(f"fill {pick.buy_fill_id} was not filled before the sell")
                if pick.quantity > float(buy["quantity"]) + _EPS:
                    raise ValidationError(f"more than fill {pick.buy_fill_id} bought")
                total += pick.quantity
            if total > float(sell["quantity"]) + _EPS:
                raise ValidationError("the picks add up to more than the sell")
            state.execute("DELETE FROM tax_lot_picks WHERE sell_fill_id = ?", [body.sell_fill_id])
            now = _now()
            for pick in body.picks:
                state.execute(
                    "INSERT INTO tax_lot_picks (portfolio_id, sell_fill_id, buy_fill_id,"
                    " quantity, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        portfolio_id,
                        body.sell_fill_id,
                        pick.buy_fill_id,
                        pick.quantity,
                        now,
                        _actor(who),
                    ],
                )
            AuditLog(state).record(
                _actor(who),
                "tax_lot_picks.set",
                "fill",
                str(body.sell_fill_id),
                portfolio_id=portfolio_id,
                details={"picks": [p.model_dump() for p in body.picks]},
                ip=ip,
            )
        return self.picks(portfolio_id, body.sell_fill_id)

    # ---- estimates (roadmap 23.5) ---------------------------------------------------

    def preview(
        self,
        portfolio_id: str,
        *,
        ticker: str,
        side: Literal["buy", "sell"],
        quantity: float,
        price: float | None = None,
        now: datetime | None = None,
    ) -> TaxPreviewView:
        """The lots a trade would close under the portfolio's lot method,
        its realised gain, holding periods, estimated tax and after-tax
        proceeds, and a US wash sale warning. Nothing is placed."""
        if not quantity > 0:
            raise ValidationError("quantity must be above zero")
        when = now or datetime.now(UTC)
        ticker = ticker.upper()
        if price is None:
            price = self._closes([ticker], when.date()).get(ticker)
            if price is None:
                raise ValidationError(f"no close for {ticker} in the lake; give the price")
        inputs = self._lot_inputs(portfolio_id)
        currency = self._currencies([ticker]).get(ticker) or inputs.base
        got = preview_trade(
            inputs.fills,
            inputs.settings,
            self._ctx.settings.tax.rates,
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=price * _multiplier(ticker),
            when=when,
            currency=currency,
            past_picks=inputs.picks,
            splits=inputs.splits,
        )
        return TaxPreviewView(
            portfolio_id=portfolio_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=price,
            currency=currency,
            jurisdiction=inputs.settings.jurisdiction,
            lot_method=inputs.settings.lot_method,
            lots=[
                PreviewLotView(
                    open_fill_id=lot.open_fill_id,
                    kind=lot.kind,  # type: ignore[arg-type]
                    quantity=lot.quantity,
                    acquired=lot.acquired,
                    holding_period=lot.holding_period,
                    cost_basis=lot.cost_basis,
                    proceeds=lot.proceeds,
                    gain=lot.gain,
                    wash_sale_disallowed=lot.wash_sale_disallowed,
                )
                for lot in got.lots
            ],
            proceeds=got.proceeds,
            realized_gain=got.realized_gain,
            short_term_gain=got.short_term_gain,
            long_term_gain=got.long_term_gain,
            wash_sale_disallowed=got.wash_sale_disallowed,
            estimated_tax=got.estimated_tax,
            after_tax_proceeds=got.after_tax_proceeds,
            year_tax_change=got.year_tax_change,
            wash_sale_warning=got.wash_sale_warning,
            rates=TaxRatesView(**got.rates.model_dump()),
        )

    def year(self, portfolio_id: str, year: int | None = None) -> TaxYearView:
        """The gains realised in ``year`` (default this year, up to today)
        and the estimated tax on them, in the base currency."""
        today = datetime.now(UTC).date()
        year = year or today.year
        _check_year(year)
        inputs = self._lot_inputs(portfolio_id)
        fx = self._fx({f.currency for f in inputs.fills if f.currency}, inputs.base)

        def to_base(amount: float, currency: str | None, day: date) -> float | None:
            return fx.convert(amount, currency or inputs.base, inputs.base, day)

        got = year_tax(
            inputs.fills,
            inputs.settings,
            self._ctx.settings.tax.rates,
            year=year,
            picks=inputs.picks,
            splits=inputs.splits,
            to_base=to_base,
        )
        return TaxYearView(
            portfolio_id=portfolio_id,
            year=year,
            base_currency=inputs.base,
            jurisdiction=inputs.settings.jurisdiction,
            short_term_gain=got.short_term_gain,
            long_term_gain=got.long_term_gain,
            wash_sale_disallowed=got.wash_sale_disallowed,
            estimated_tax=got.estimated_tax,
            disposals=got.disposals,
            unconverted=got.unconverted,
            rates=TaxRatesView(**got.rates.model_dump()),
        )

    # ---- exports ----------------------------------------------------------------------

    def gains_csv(self, portfolio_id: str, year: int) -> str:
        """Realized gains per lot disposed in ``year`` (see ``stonks.tax.lots``)."""
        _check_year(year)
        inputs = self._lot_inputs(portfolio_id)
        disposals = realized_disposals(inputs.fills, inputs.settings, inputs.picks, inputs.splits)
        fx = self._fx({f.currency for f in inputs.fills if f.currency}, inputs.base)
        return to_csv(GAINS_COLUMNS, gains_rows(disposals, year, inputs.base, fx))

    def open_lots_csv(self, portfolio_id: str, as_of: date | None = None) -> str:
        """The lots still open at the end of ``as_of`` (default today, UTC):
        cost basis, days held, holding period, and the value and unrealized
        gain at the latest close on or before that day (roadmap 13.12)."""
        day = as_of or datetime.now(UTC).date()
        if not date(1900, 1, 1) <= day <= date(2200, 12, 31):
            raise ValidationError("as_of must be between 1900 and 2200")
        inputs = self._lot_inputs(portfolio_id)
        lots = open_lots(inputs.fills, day, inputs.settings, inputs.picks, inputs.splits)
        prices = self._closes(sorted({lot.ticker for lot in lots}), day)
        currencies = {lot.currency for lot in lots if lot.currency}
        fx = self._fx(currencies, inputs.base)
        return to_csv(OPEN_LOT_COLUMNS, open_lot_rows(lots, day, inputs.base, fx, prices))

    def open_lots(self, portfolio_id: str, as_of: date) -> list[OpenLot]:
        """The lots still open at the end of ``as_of`` (the planner's tax
        preview, roadmap 23.16). The caller has resolved the portfolio."""
        inputs = self._lot_inputs(portfolio_id)
        return open_lots(inputs.fills, as_of, inputs.settings, inputs.picks, inputs.splits)

    def _lot_inputs(self, portfolio_id: str) -> _LotInputs:
        """The portfolio's fills (fees in the trade currency), lot picks,
        splits and tax settings: what the lot replay reads."""
        with self._ctx.state() as state:
            view = _settings_view(state, portfolio_id)
            raw = state.sql(
                "SELECT f.id, f.ticker, f.quantity, f.price, f.fee, f.fee_currency, f.filled_at,"
                " o.side"
                " FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
                " WHERE f.portfolio_id = ? ORDER BY f.filled_at, f.id",
                [portfolio_id],
            )
            pick_rows = state.sql(
                "SELECT sell_fill_id, buy_fill_id, quantity FROM tax_lot_picks"
                " WHERE portfolio_id = ? ORDER BY sell_fill_id, buy_fill_id",
                [portfolio_id],
            )
            split_rows = state.sql(
                "SELECT ticker, ex_date, value FROM corporate_action_ledger"
                " WHERE portfolio_id = ? AND kind = 'split' ORDER BY ex_date, ticker",
                [portfolio_id],
            )
        splits = [
            TaxSplit(
                ticker=r["ticker"],
                ex_date=date.fromisoformat(str(r["ex_date"])[:10]),
                ratio=float(r["value"]),
            )
            for r in split_rows
        ]
        currencies = self._currencies(sorted({r["ticker"] for r in raw}))
        fee_fx = self._fx(
            {*currencies.values(), *(r["fee_currency"] for r in raw if r["fee_currency"])},
            view.base_currency,
        )

        def fee_of(r: Any) -> float:
            """The fee in the trade's currency (a broker may report the
            commission in another one, ``fills.fee_currency``)."""
            fee = float(r["fee"] or 0.0)
            source = r["fee_currency"]
            target = currencies.get(r["ticker"]) or view.base_currency
            if not fee or not source or source.upper() == target.upper():
                return fee
            day = _utc(r["filled_at"]).date()
            converted = fee_fx.convert(fee, source.upper(), target.upper(), day)
            if converted is None:
                raise ValidationError(
                    f"no FX rate from {source} to {target} on or before {day} for the "
                    f"commission of fill {r['id']}; ingest the rate (stonks ingest fx)"
                )
            return converted

        fills = [
            TaxFill(
                id=int(r["id"]),
                ticker=r["ticker"],
                side=r["side"],
                quantity=float(r["quantity"]),
                # an option fill's price is per share of the deliverable:
                # the lot counts it per contract
                price=float(r["price"]) * _multiplier(r["ticker"]),
                fee=fee_of(r),
                filled_at=_utc(r["filled_at"]),
                currency=currencies.get(r["ticker"]),
            )
            for r in raw
        ]
        picks: dict[int, list[tuple[int, float]]] = {}
        for r in pick_rows:
            picks.setdefault(int(r["sell_fill_id"]), []).append(
                (int(r["buy_fill_id"]), float(r["quantity"]))
            )
        return _LotInputs(
            base=view.base_currency,
            settings=TaxSettings(
                jurisdiction=view.jurisdiction,
                lot_method=view.lot_method,
                wash_sales=view.wash_sales,
            ),
            fills=fills,
            picks=picks,
            splits=splits,
        )

    def dividends_csv(self, portfolio_id: str, year: int) -> str:
        """Dividends with an ex-date in ``year``, from the corporate action
        ledger: gross = per share x shares held, net = the cash credited,
        withholding = gross - net."""
        _check_year(year)
        with self._ctx.state() as state:
            view = _settings_view(state, portfolio_id)
            rows = state.sql(
                "SELECT ticker, ex_date, value, quantity_before, cash_delta"
                " FROM corporate_action_ledger WHERE portfolio_id = ? AND kind = 'dividend'"
                " AND ex_date >= ? AND ex_date <= ? ORDER BY ex_date, ticker",
                [portfolio_id, f"{year}-01-01", f"{year}-12-31"],
            )
        currencies = self._currencies(sorted({r["ticker"] for r in rows}))
        events = [
            DividendEvent(
                ticker=r["ticker"],
                ex_date=date.fromisoformat(str(r["ex_date"])[:10]),
                per_share=float(r["value"]),
                quantity=float(r["quantity_before"]),
                net=float(r["cash_delta"]),
                currency=currencies.get(r["ticker"]),
            )
            for r in rows
            if abs(float(r["quantity_before"])) > _EPS
        ]
        fx = self._fx({e.currency for e in events if e.currency}, view.base_currency)
        return to_csv(DIVIDEND_COLUMNS, dividend_rows(events, year, view.base_currency, fx))

    # ---- lake reads -----------------------------------------------------------------

    def _currencies(self, tickers: list[str]) -> dict[str, str]:
        if not tickers:
            return {}
        with self._ctx.lake() as lake:
            df = lake.sql(
                "SELECT id, currency FROM instruments WHERE id = ANY(?) AND currency IS NOT NULL",
                [tickers],
            )
        return {str(i): str(c) for i, c in zip(df["id"], df["currency"], strict=True)}

    def _closes(self, tickers: list[str], day: date) -> dict[str, float]:
        """The latest daily close on or before ``day`` per ticker."""
        if not tickers:
            return {}
        with self._ctx.lake() as lake:
            df = lake.sql(
                "SELECT ticker, arg_max(close, date) AS close FROM prices"
                " WHERE ticker = ANY(?) AND date <= ? AND close IS NOT NULL GROUP BY ticker",
                [tickers, day],
            )
        return {str(r["ticker"]): float(r["close"]) for r in df.to_dict("records")}

    def _fx(self, currencies: set[str], base: str) -> FxRates:
        if not currencies - {base}:
            return FxRates([])
        with self._ctx.lake() as lake:
            return load_fx_rates(lake, {*currencies, base})


@dataclass(frozen=True)
class _LotInputs:
    base: str
    settings: TaxSettings
    fills: list[TaxFill]
    picks: dict[int, list[tuple[int, float]]]
    splits: list[TaxSplit]


def _check_year(year: int) -> None:
    if not 1900 <= year <= 2200:
        raise ValidationError("year must be between 1900 and 2200")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fill(state: SqliteState, portfolio_id: str, fill_id: int) -> Any:
    rows = state.sql(
        "SELECT f.id, f.ticker, f.quantity, f.filled_at, o.side FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id"
        " WHERE f.id = ? AND f.portfolio_id = ?",
        [fill_id, portfolio_id],
    )
    return rows[0] if rows else None


def _refuse_locked_change(
    state: SqliteState, portfolio_id: str, current: TaxSettingsView, merged: TaxSettingsView
) -> None:
    """The jurisdiction and base currency are the live account profile's
    too (migration 040), so they are locked while the portfolio trades real
    money, like the profile."""
    from stonks.production.live.stages import get_stage, trades_real_money

    changed = (merged.jurisdiction, merged.base_currency) != (
        current.jurisdiction,
        current.base_currency,
    )
    stage = get_stage(state, portfolio_id)
    if changed and trades_real_money(stage):
        raise ConflictError(
            "the jurisdiction and base currency are locked while the portfolio trades real"
            f" money ({stage}): demote it to broker_paper to change them"
        )


def _is_locked(state: SqliteState, portfolio_id: str) -> bool:
    """Whether the portfolio's stage trades real money (see ``_refuse_locked_change``)."""
    from stonks.production.live.stages import get_stage, trades_real_money

    return trades_real_money(get_stage(state, portfolio_id))


def _settings_view(state: SqliteState, portfolio_id: str) -> TaxSettingsView:
    base = state.sql("SELECT base_currency FROM portfolios WHERE id = ?", [portfolio_id])
    row = state.sql(
        "SELECT jurisdiction, lot_method, wash_sales, updated_at FROM portfolio_tax_settings"
        " WHERE portfolio_id = ?",
        [portfolio_id],
    )
    currency = str(base[0]["base_currency"]).upper() if base else "USD"
    locked = _is_locked(state, portfolio_id)
    if not row:
        return TaxSettingsView(
            portfolio_id=portfolio_id,
            base_currency=currency,
            jurisdiction="us",
            lot_method="fifo",
            wash_sales=True,
            locked=locked,
        )
    r = row[0]
    return TaxSettingsView(
        portfolio_id=portfolio_id,
        base_currency=currency,
        jurisdiction=r["jurisdiction"],
        lot_method=r["lot_method"],
        wash_sales=bool(r["wash_sales"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
        locked=locked,
    )


def _multiplier(ticker: str) -> float:
    """Money per unit of price for one unit held: an option contract's
    multiplier, 1 for anything else."""
    from stonks.core.instruments import InstrumentBook

    return InstrumentBook().multiplier(ticker)
