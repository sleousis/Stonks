"""TaxService (roadmap 20.5): a portfolio's tax settings, specific-lot
picks, and yearly CSV exports of realized gains and dividends.

Callers resolve ``portfolio_id`` to one the caller owns first
(``PortfolioService.resolve`` in the API, ``owned_portfolio`` in the CLI).
Writes take the caller (a :class:`Principal` from the API, or a bare
:class:`Scope` from the CLI) and write an ``audit_log`` row."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from stonks.accounts import Scope
from stonks.accounts.audit import AuditLog
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.fx import FxRates, load_fx_rates
from stonks.store.state import SqliteState
from stonks.tax import (
    DIVIDEND_COLUMNS,
    GAINS_COLUMNS,
    DividendEvent,
    TaxFill,
    TaxSettings,
    TaxSplit,
    dividend_rows,
    gains_rows,
    realized_disposals,
    to_csv,
)

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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _actor(who: Who) -> str:
    return who.actor


def _check_write(who: Who) -> None:
    if isinstance(who, Principal):
        require(who, Permission.PORTFOLIO_MANAGE)


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

    # ---- exports ----------------------------------------------------------------------

    def gains_csv(self, portfolio_id: str, year: int) -> str:
        """Realized gains per lot disposed in ``year`` (see ``stonks.tax.lots``)."""
        _check_year(year)
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
                price=float(r["price"]),
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
        disposals = realized_disposals(
            fills,
            TaxSettings(
                jurisdiction=view.jurisdiction,
                lot_method=view.lot_method,
                wash_sales=view.wash_sales,
            ),
            picks,
            splits,
        )
        fx = self._fx({f.currency for f in fills if f.currency}, view.base_currency)
        return to_csv(GAINS_COLUMNS, gains_rows(disposals, year, view.base_currency, fx))

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

    def _fx(self, currencies: set[str], base: str) -> FxRates:
        if not currencies - {base}:
            return FxRates([])
        with self._ctx.lake() as lake:
            return load_fx_rates(lake, {*currencies, base})


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


def _settings_view(state: SqliteState, portfolio_id: str) -> TaxSettingsView:
    base = state.sql("SELECT base_currency FROM portfolios WHERE id = ?", [portfolio_id])
    row = state.sql(
        "SELECT jurisdiction, lot_method, wash_sales, updated_at FROM portfolio_tax_settings"
        " WHERE portfolio_id = ?",
        [portfolio_id],
    )
    currency = str(base[0]["base_currency"]).upper() if base else "USD"
    if not row:
        return TaxSettingsView(
            portfolio_id=portfolio_id,
            base_currency=currency,
            jurisdiction="us",
            lot_method="fifo",
            wash_sales=True,
        )
    r = row[0]
    return TaxSettingsView(
        portfolio_id=portfolio_id,
        base_currency=currency,
        jurisdiction=r["jurisdiction"],
        lot_method=r["lot_method"],
        wash_sales=bool(r["wash_sales"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )
