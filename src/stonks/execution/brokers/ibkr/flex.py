"""IBKR Flex statements over the Flex Web Service (roadmap 19.3).

A Flex statement is IBKR's official record of an account: every execution
with its commission, and cash transactions (dividends, interest, fees,
deposits). The socket only reaches back about a day, so the statement is
the backstop for older activity and, later, for reconciliation.

The Web Service takes two steps:

1. ``SendRequest?t=<token>&q=<query id>&v=3`` answers a reference code;
2. ``GetStatement?t=<token>&q=<reference>&v=3`` answers the statement, or
   error 1019 while IBKR still generates it (we poll).

It is optional and off until ``[brokers.ibkr.flex] query_id`` is set and
``STONKS_IBKR_FLEX_TOKEN`` is in the environment. The token is never
logged, stored or shown: error text is scrubbed of it. The transport is
injected in tests. The XML is parsed with the standard library, whose
expat refuses entity expansion attacks and never fetches external entities.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

from stonks.execution.brokers.ibkr.settings import IbkrFlexSettings
from stonks.ingest.redact import redact_secrets
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.ibkr.flex")

FLEX_TOKEN_ENV = "STONKS_IBKR_FLEX_TOKEN"
#: Error codes: 1019 generating (poll), 1018 too many requests (poll).
_NOT_READY = frozenset({"1018", "1019"})
#: Error codes that mean the token or its service is refused.
_AUTH = frozenset({"1011", "1012", "1015"})
#: Trade rows that summarise executions (kept only without execution rows).
_SUMMARY_LEVELS = frozenset({"ORDER", "CLOSED_LOT", "SYMBOL_SUMMARY", "ASSET_SUMMARY"})

Transport = Callable[[str, Mapping[str, str]], str]


class FlexError(RuntimeError):
    """A Flex fetch or parse failed. The text never holds the token."""


class FlexAuthError(FlexError):
    """IBKR refused the Flex token (expired, invalid or service inactive)."""


@dataclass(frozen=True)
class FlexTrade:
    """One execution as the statement reports it. ``quantity`` is signed
    (a sell is negative). ``commission`` is IBKR's sign (a cost is negative)."""

    trade_id: str
    exec_id: str | None
    account_id: str
    symbol: str
    quantity: float
    price: float
    trade_date: date | None
    currency: str | None = None
    asset_class: str | None = None
    con_id: int | None = None
    isin: str | None = None
    listing_exchange: str | None = None
    settle_date: date | None = None
    proceeds: float | None = None
    commission: float | None = None
    commission_currency: str | None = None
    order_ref: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class FlexCashTransaction:
    """One cash movement. ``type`` is IBKR's own (``Dividends``,
    ``Broker Interest Received``, ``Deposits/Withdrawals``, ...)."""

    transaction_id: str
    account_id: str
    type: str
    amount: float
    date: date | None
    currency: str | None = None
    settle_date: date | None = None
    symbol: str | None = None
    con_id: int | None = None
    description: str | None = None


@dataclass(frozen=True)
class FlexStatement:
    account_id: str
    from_date: date | None
    to_date: date | None
    trades: tuple[FlexTrade, ...] = ()
    cash_transactions: tuple[FlexCashTransaction, ...] = ()


# ---- parsing --------------------------------------------------------------------------


def parse_flex_statements(xml_text: str) -> list[FlexStatement]:
    """Every ``FlexStatement`` of a ``FlexQueryResponse``."""
    root = _xml(xml_text)
    if root.tag != "FlexQueryResponse":
        raise FlexError(f"not a Flex statement (root element {root.tag!r})")
    return [_statement(el) for el in root.iter("FlexStatement")]


def _xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text.strip())
    except ET.ParseError as exc:
        raise FlexError(f"Flex answered something that is not XML ({exc})") from None


def _statement(el: ET.Element) -> FlexStatement:
    rows = list(el.iter("Trade"))
    executions = [r for r in rows if (r.get("levelOfDetail") or "").upper() == "EXECUTION"]
    if executions:
        rows = executions
    else:
        rows = [r for r in rows if (r.get("levelOfDetail") or "").upper() not in _SUMMARY_LEVELS]
    trades = tuple(t for t in (_trade(r) for r in rows) if t is not None)
    cash = tuple(c for c in (_cash(r) for r in el.iter("CashTransaction")) if c is not None)
    return FlexStatement(
        account_id=el.get("accountId") or "",
        from_date=_date(el.get("fromDate")),
        to_date=_date(el.get("toDate")),
        trades=trades,
        cash_transactions=cash,
    )


def _trade(r: ET.Element) -> FlexTrade | None:
    trade_id = _text(r.get("tradeID"))
    exec_id = _text(r.get("ibExecID"))
    quantity = _num(r.get("quantity"))
    price = _num(r.get("tradePrice"))
    if (trade_id is None and exec_id is None) or quantity is None or price is None:
        return None
    return FlexTrade(
        trade_id=trade_id or exec_id or "",
        exec_id=exec_id,
        account_id=r.get("accountId") or "",
        symbol=r.get("symbol") or "",
        quantity=quantity,
        price=price,
        trade_date=_date(r.get("tradeDate")),
        currency=_text(r.get("currency")),
        asset_class=_text(r.get("assetCategory")),
        con_id=_int(r.get("conid")),
        isin=_text(r.get("isin")),
        listing_exchange=_text(r.get("listingExchange")),
        settle_date=_date(r.get("settleDateTarget")),
        proceeds=_num(r.get("proceeds")),
        commission=_num(r.get("ibCommission")),
        commission_currency=_text(r.get("ibCommissionCurrency")),
        order_ref=_text(r.get("orderReference")),
        description=_text(r.get("description")),
    )


def _cash(r: ET.Element) -> FlexCashTransaction | None:
    tid = _text(r.get("transactionID"))
    amount = _num(r.get("amount"))
    if tid is None or amount is None:
        return None
    return FlexCashTransaction(
        transaction_id=tid,
        account_id=r.get("accountId") or "",
        type=r.get("type") or "",
        amount=amount,
        date=_date(r.get("dateTime") or r.get("reportDate")),
        currency=_text(r.get("currency")),
        settle_date=_date(r.get("settleDate")),
        symbol=_text(r.get("symbol")),
        con_id=_int(r.get("conid")),
        description=_text(r.get("description")),
    )


def _text(value: str | None) -> str | None:
    clean = (value or "").strip()
    return clean or None


def _num(value: str | None) -> float | None:
    try:
        return float((value or "").replace(",", ""))
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    """``20260925``, ``2026-09-25`` or either with ``;HHMMSS`` / a time."""
    raw = (value or "").strip().split(";")[0].split(" ")[0].split("T")[0]
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ---- the Web Service ------------------------------------------------------------------


def _requests_transport(timeout: float) -> Transport:
    import requests

    def get(url: str, params: Mapping[str, str]) -> str:
        response = requests.get(
            url, params=dict(params), timeout=timeout, headers={"User-Agent": "stonks/1"}
        )
        response.raise_for_status()
        return response.text

    return get


@dataclass
class FlexClient:
    token: str = field(repr=False)
    settings: IbkrFlexSettings
    transport: Transport | None = field(default=None, repr=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def __post_init__(self) -> None:
        if not self.token:
            raise FlexError(f"Flex needs a token in {FLEX_TOKEN_ENV}")
        if not self.settings.query_id:
            raise FlexError("Flex needs [brokers.ibkr.flex] query_id")
        if self.transport is None:
            self.transport = _requests_transport(self.settings.timeout_seconds)

    @classmethod
    def from_env(
        cls,
        settings: IbkrFlexSettings,
        environ: Mapping[str, str] | None = None,
        **kw: object,
    ) -> FlexClient | None:
        """A client when Flex is configured, else ``None`` (it is optional)."""
        env = os.environ if environ is None else environ
        token = (env.get(FLEX_TOKEN_ENV) or "").strip()
        if not token or not settings.query_id:
            return None
        return cls(token, settings, **kw)  # type: ignore[arg-type]

    def fetch(self) -> list[FlexStatement]:
        """Run the query and return its statements. Raises ``FlexError``."""
        base = self.settings.base_url.rstrip("/")
        send = self._answer(
            f"{base}/SendRequest", {"t": self.token, "q": str(self.settings.query_id), "v": "3"}
        )
        root = _xml(send)
        self._raise_for(root, step="request")
        reference = _text(root.findtext("ReferenceCode"))
        if reference is None:
            raise FlexError("Flex answered no reference code")
        url = _text(root.findtext("Url")) or f"{base}/GetStatement"
        for attempt in range(self.settings.max_polls):
            body = self._answer(url, {"t": self.token, "q": reference, "v": "3"})
            root = _xml(body)
            if root.tag == "FlexStatementResponse":
                code = _text(root.findtext("ErrorCode"))
                if code in _NOT_READY:
                    _log.info("ibkr.flex.not_ready", attempt=attempt + 1, code=code)
                    self.sleep(self.settings.poll_seconds)
                    continue
                self._raise_for(root, step="statement")
                raise FlexError("Flex answered neither a statement nor an error")
            statements = parse_flex_statements(body)
            _log.info("ibkr.flex.fetched", statements=len(statements))
            return statements
        raise FlexError(f"Flex statement not ready after {self.settings.max_polls} polls")

    def _answer(self, url: str, params: Mapping[str, str]) -> str:
        assert self.transport is not None
        try:
            return self.transport(url, params)
        except Exception as exc:  # the transport's text may hold the URL and token
            raise FlexError(
                f"Flex request failed: {self._scrub(f'{type(exc).__name__}: {exc}')}"
            ) from None

    def _raise_for(self, root: ET.Element, *, step: str) -> None:
        if root.tag != "FlexStatementResponse":
            return
        status = (_text(root.findtext("Status")) or "").lower()
        code = _text(root.findtext("ErrorCode"))
        if status == "success" and code is None:
            return
        message = self._scrub(_text(root.findtext("ErrorMessage")) or "no message")
        text = f"Flex {step} refused (error {code}): {message}"
        if code in _AUTH:
            raise FlexAuthError(text)
        raise FlexError(text)

    def _scrub(self, text: str) -> str:
        return redact_secrets(text, [self.token])
