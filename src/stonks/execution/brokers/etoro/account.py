"""One eToro account (demo or real): its endpoints and the reads of its
portfolio, parsed into our types.

The P&L endpoint answers positions, pending orders and the available
credit in one call. Equity follows eToro's own guide (Calculate Equity):
available cash plus total invested plus unrealized P&L, in USD.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from stonks.execution.brokers.etoro._json import integer, num, obj, rows, strings
from stonks.execution.brokers.etoro.client import EtoroClient

Env = Literal["demo", "real"]
ENVS: tuple[Env, ...] = ("demo", "real")

#: eToro's ``settlementTypeID`` values (Get account PnL and portfolio details).
SETTLEMENT_CFD = 0
SETTLEMENT_REAL = 1
_HISTORY_PAGE = 100
_MAX_HISTORY_PAGES = 50


class EtoroPaths:
    """The endpoint paths of one environment."""

    def __init__(self, env: Env) -> None:
        self.env: Env = env
        demo = env == "demo"
        self.pnl = f"/api/v1/trading/info/{env}/pnl"
        self.history = "/api/v1/trading/info/trade/" + ("demo/history" if demo else "history")
        self.open = "/api/v2/trading/execution/" + ("demo/orders" if demo else "orders")
        self.lookup = "/api/v2/trading/info/" + ("demo/orders:lookup" if demo else "orders:lookup")
        self.eligibility = "/api/v2/trading/info/" + ("demo/eligibility" if demo else "eligibility")
        close = "/api/v1/trading/execution/" + ("demo/" if demo else "")
        self.close_base = f"{close}market-close-orders"
        self.close_info = f"/api/v1/trading/info/{env}/close-orders"
        self.rates = "/api/v2/market-data/rates"
        self.me = "/api/v1/me"

    def close_position(self, position_id: int) -> str:
        return f"{self.close_base}/positions/{position_id}"

    def cancel_close(self, order_id: int) -> str:
        return f"{self.close_base}/{order_id}"

    def cancel_open(self, order_id: int) -> str:
        return f"{self.open}/{order_id}"


@dataclass(frozen=True)
class EtoroPosition:
    position_id: int
    instrument_id: int
    units: float
    open_rate: float
    opened_at: datetime | None
    is_buy: bool
    leverage: int
    settlement: int
    #: Non-zero: the position belongs to a copied trader (a mirror).
    mirror_id: int
    #: USD invested (with any extra margin).
    amount: float
    initial_units: float
    initial_amount: float
    #: Unrealized P&L in USD, when eToro sent it.
    pnl: float | None
    #: The price the position would close at now (asset currency).
    close_rate: float | None
    conversion_rate: float | None
    order_id: int

    @property
    def plain(self) -> bool:
        """A long, unleveraged position in the real asset, outside any copy:
        the only kind the trader opens and closes."""
        return (
            self.is_buy
            and self.leverage == 1
            and self.settlement == SETTLEMENT_REAL
            and self.mirror_id == 0
        )

    @property
    def signed_units(self) -> float:
        return self.units if self.is_buy else -self.units


@dataclass(frozen=True)
class PendingOpen:
    order_id: int
    instrument_id: int
    units: float | None
    amount: float
    is_buy: bool


@dataclass(frozen=True)
class PendingClose:
    order_id: int
    instrument_id: int
    position_id: int
    units: float | None


@dataclass(frozen=True)
class EtoroSnapshot:
    #: The account's own positions, outside copies.
    positions: tuple[EtoroPosition, ...]
    #: Positions held inside copied traders (read only for Stonks).
    mirror_positions: tuple[EtoroPosition, ...]
    credit: float
    available_cash: float
    equity: float
    pending_open: tuple[PendingOpen, ...]
    pending_close: tuple[PendingClose, ...]


class EtoroAccount:
    """Reads of one environment's account through ``client``."""

    def __init__(self, client: EtoroClient, env: Env) -> None:
        if env not in ENVS:
            raise ValueError(f"unknown eToro environment {env!r}")
        self.client = client
        self.env: Env = env
        self.paths = EtoroPaths(env)

    def account_id(self) -> tuple[str, tuple[str, ...]]:
        """The account's id (``demo-<cid>`` or ``real-<cid>``) and the key's
        scopes when eToro lists them. Nothing else of the profile is kept:
        it holds personal data Stonks does not need."""
        me = obj(self.client.get("identity", self.paths.me))
        cid: object = me.get("demoCid" if self.env == "demo" else "realCid")
        scopes = tuple(strings(me.get("scopes")))
        if cid in (None, "", 0):
            cid = me.get("gcid") or "account"
        return f"{self.env}-{cid}", scopes

    def snapshot(self) -> EtoroSnapshot:
        data = self.client.get("portfolio", self.paths.pnl)
        return parse_portfolio(data)

    def history(self, since: date) -> list[dict[str, Any]]:
        """Closed trades since ``since``, oldest page first."""
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_HISTORY_PAGES + 1):
            answer: object = self.client.get(
                "trade history",
                self.paths.history,
                {"minDate": since.isoformat(), "page": page, "pageSize": _HISTORY_PAGE},
            )
            wrapped = obj(answer)
            if wrapped:  # some answers wrap the list
                answer = wrapped.get("items") or wrapped.get("results") or []
            batch = rows(answer)
            out.extend(batch)
            if len(batch) < _HISTORY_PAGE:
                break
        return out


def parse_portfolio(data: object) -> EtoroSnapshot:
    top = obj(data)
    root = obj(top.get("clientPortfolio", top))
    positions = tuple(p for p in (_position(r) for r in _rows(root, "positions")) if p)
    mirrors = _rows(root, "mirrors")
    mirror_positions = tuple(
        p for m in mirrors for p in (_position(r) for r in _rows(m, "positions")) if p
    )
    open_own = [
        r
        for r in _rows(root, "ordersForOpen")
        if _int(r.get("mirrorId", r.get("mirrorID")), default=0) == 0
    ]
    queued = _rows(root, "orders")
    credit = _num(root.get("credit", root.get("credits"))) or 0.0
    reserved = sum(_num(r.get("amount")) or 0.0 for r in open_own) + sum(
        _num(r.get("amount")) or 0.0 for r in queued
    )
    available = credit - reserved
    invested = (
        sum(p.amount for p in positions)
        + sum(p.amount for p in mirror_positions)
        + sum(
            (_num(m.get("availableAmount")) or 0.0)
            - (_num(m.get("closedPositionsNetProfit")) or 0.0)
            for m in mirrors
        )
        + reserved
        + sum(_num(r.get("totalExternalCosts")) or 0.0 for r in open_own)
    )
    unrealized = (
        sum(p.pnl or 0.0 for p in positions)
        + sum(p.pnl or 0.0 for p in mirror_positions)
        + sum(_num(m.get("closedPositionsNetProfit")) or 0.0 for m in mirrors)
    )
    pending_open = tuple(
        PendingOpen(
            order_id=_int(r.get("orderId", r.get("orderID"))),
            instrument_id=_int(r.get("instrumentId", r.get("instrumentID"))),
            units=_num(r.get("amountInUnits", r.get("units"))),
            amount=_num(r.get("amount")) or 0.0,
            is_buy=bool(r.get("isBuy", True)),
        )
        for r in [*open_own, *queued]
    )
    pending_close = tuple(
        PendingClose(
            order_id=_int(r.get("orderId", r.get("orderID"))),
            instrument_id=_int(r.get("instrumentId", r.get("instrumentID"))),
            position_id=_int(r.get("positionId", r.get("positionID"))),
            units=_num(r.get("unitsToDeduct")),
        )
        for r in _rows(root, "ordersForClose")
    )
    return EtoroSnapshot(
        positions=positions,
        mirror_positions=mirror_positions,
        credit=credit,
        available_cash=available,
        equity=available + invested + unrealized,
        pending_open=pending_open,
        pending_close=pending_close,
    )


def _position(row: dict[str, Any]) -> EtoroPosition | None:
    pid = _int(row.get("positionID", row.get("positionId")))
    iid = _int(row.get("instrumentID", row.get("instrumentId")))
    units = _num(row.get("units"))
    if pid < 0 or iid < 0 or units is None:
        return None
    pnl = obj(row.get("unrealizedPnL"))
    return EtoroPosition(
        position_id=pid,
        instrument_id=iid,
        units=units,
        open_rate=_num(row.get("openRate")) or 0.0,
        opened_at=parse_time(row.get("openDateTime")),
        is_buy=bool(row.get("isBuy", True)),
        leverage=_int(row.get("leverage"), default=1),
        settlement=_int(row.get("settlementTypeID"), default=SETTLEMENT_REAL),
        mirror_id=_int(row.get("mirrorID"), default=0),
        amount=_num(row.get("amount")) or 0.0,
        initial_units=_num(row.get("initialUnits")) or units,
        initial_amount=_num(row.get("initialAmountInDollars")) or 0.0,
        pnl=_num(pnl.get("pnL")),
        close_rate=_num(pnl.get("closeRate")),
        conversion_rate=_num(row.get("openConversionRate")),
        order_id=_int(row.get("orderID"), default=0),
    )


def _rows(parent: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return rows(parent.get(key))


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


_num = num
_int = integer


__all__ = [
    "ENVS",
    "Env",
    "EtoroAccount",
    "EtoroPaths",
    "EtoroPosition",
    "EtoroSnapshot",
    "PendingClose",
    "PendingOpen",
    "parse_portfolio",
    "parse_time",
]
