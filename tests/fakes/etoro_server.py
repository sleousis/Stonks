"""A fake eToro public API for hermetic tests, served through an httpx2
``MockTransport`` (:meth:`FakeEtoro.transport`). No test ever calls eToro.

It speaks the JSON shapes of eToro's documented endpoints
(``docs/design/etoro.md``) for one account, demo or real:

- the key pair must match, and a demo key only reaches demo endpoints
  (a real key only real ones), else ``403``
- ``x-request-id`` must be a UUID, and an order create is idempotent by it
- ``fill_mode`` scripts what an order does: ``fill`` at once, ``wait``
  for the market (:meth:`release` fills it later), ``partial`` fills half,
  ``reject`` refuses it
- ``script`` queues canned answers (a 429 with ``Retry-After``, a 500)
  for the next calls to a path.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx2

API_KEY = "pub-key-ABCDEF-123456"
USER_KEY = "user-key-SECRET-7890XYZ"
NOW = "2026-03-03T16:00:00Z"


@dataclass
class FakeInstrument:
    id: int
    symbol: str
    type: str = "Stocks"
    exchange_id: int = 4
    #: Settlement types a long, leverage 1 position may use.
    settlements: tuple[str, ...] = ("real", "cfd")
    units_type: str = "fractional"
    quantity_type: str = "all"
    min_exposure: float = 10.0
    max_units: float = 100_000.0
    allow_open: bool = True
    allow_partial_close: bool = True
    bid: float = 100.0
    ask: float = 100.0


def default_instruments() -> dict[int, FakeInstrument]:
    items = [
        FakeInstrument(1001, "AAPL", exchange_id=4, bid=199.5, ask=200.0),
        FakeInstrument(1002, "MSFT", exchange_id=4, bid=399.0, ask=400.0),
        FakeInstrument(1003, "BARC.L", exchange_id=7, bid=2.0, ask=2.01),
        FakeInstrument(100000, "BTC", type="Crypto", exchange_id=8, bid=60000.0, ask=60010.0),
        FakeInstrument(1, "EURUSD", type="Forex", exchange_id=1, settlements=("cfd",)),
        FakeInstrument(1004, "CFDONLY", exchange_id=4, settlements=("cfd",)),
        FakeInstrument(1005, "WHOLE", exchange_id=5, units_type="whole"),
        FakeInstrument(1006, "AMTONLY", exchange_id=5, quantity_type="amountOnly", ask=50.0),
    ]
    return {i.id: i for i in items}


EXCHANGES = {
    1: "FX",
    4: "Nasdaq",
    5: "NYSE",
    7: "London",
    8: "Digital Currency",
}


@dataclass
class FakeEtoro:
    env: str = "demo"
    api_key: str = API_KEY
    user_key: str = USER_KEY
    cid: int = 4242
    credit: float = 10_000.0
    fill_mode: str = "fill"
    instruments: dict[int, FakeInstrument] = field(default_factory=default_instruments)
    positions: list[dict[str, Any]] = field(default_factory=list)
    mirrors: list[dict[str, Any]] = field(default_factory=list)
    orders: dict[int, dict[str, Any]] = field(default_factory=dict)
    close_orders: dict[int, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    scopes: list[str] | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    headers_seen: list[dict[str, str]] = field(default_factory=list)
    script: dict[str, deque[tuple[int, dict[str, str], Any]]] = field(default_factory=dict)
    _next_id: int = 5000

    # ---- scripting ------------------------------------------------------------

    def queue(self, path: str, status: int, headers: dict[str, str] | None = None,
              body: Any = None) -> None:  # fmt: skip
        """The next call to ``path`` answers ``status`` (then normal again)."""
        self.script.setdefault(path, deque()).append((status, headers or {}, body))

    def add_position(
        self,
        instrument_id: int,
        units: float,
        rate: float,
        *,
        opened: str = "2026-01-05T15:00:00Z",
        is_buy: bool = True,
        leverage: int = 1,
        settlement: int = 1,
        order_id: int = 0,
    ) -> int:
        pid = self._id()
        self.positions.append(
            {
                "positionID": pid,
                "CID": self.cid,
                "openDateTime": opened,
                "openRate": rate,
                "instrumentID": instrument_id,
                "mirrorID": 0,
                "isBuy": is_buy,
                "amount": units * rate / leverage,
                "leverage": leverage,
                "orderID": order_id,
                "units": units,
                "initialUnits": units,
                "initialAmountInDollars": units * rate / leverage,
                "totalFees": 0.0,
                "settlementTypeID": settlement,
                "openConversionRate": 1.0,
                "unrealizedPnL": {"pnL": 0.0, "closeRate": self.instruments[instrument_id].bid
                                  if instrument_id in self.instruments else rate},
            }
        )  # fmt: skip
        return pid

    def release(self, order_id: int) -> None:
        """The market opened: a waiting open or close order fills."""
        if order_id in self.orders:
            order = self.orders[order_id]
            self._execute_open(order, order["requestedUnits"])
        else:
            self._execute_close(self.close_orders[order_id])

    # ---- transport ------------------------------------------------------------

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        self.headers_seen.append({k.lower(): v for k, v in request.headers.items()})
        queued = self.script.get(path)
        if queued:
            status, headers, body = queued.popleft()
            return httpx2.Response(status, headers=headers, json=body or {"errorCode": status})
        if request.headers.get("x-api-key") != self.api_key or request.headers.get(
            "x-user-key"
        ) != self.user_key:
            return _error(401, "Unauthorized", "invalid credentials")
        try:
            uuid.UUID(request.headers.get("x-request-id", ""))
        except ValueError:
            return _error(422, "RequestIdRequired", "x-request-id must be a UUID")
        other = "real" if self.env == "demo" else "demo"
        if f"/{other}/" in path or path.endswith(f"/{other}") or (
            self.env == "demo" and _real_only(path)
        ):
            return _error(403, "Forbidden", "this key is for another environment")
        query = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content) if request.content else {}
        return self._route(method, path, query, body, request.headers["x-request-id"])

    # ---- routes ---------------------------------------------------------------

    def _route(
        self, method: str, path: str, q: dict[str, str], body: Any, request_id: str
    ) -> httpx2.Response:
        env = self.env
        exe = "/api/v2/trading/execution" + ("/demo" if env == "demo" else "")
        close = "/api/v1/trading/execution" + ("/demo" if env == "demo" else "")
        if method == "GET" and path == "/api/v1/me":
            return _ok({"gcid": 1, "realCid": self.cid + 1 if env == "demo" else self.cid,
                        "demoCid": self.cid if env == "demo" else self.cid + 1,
                        "username": "private-username", "firstName": "Private",
                        "lastName": "Person", "dateOfBirth": "1990-01-01",
                        "scopes": self.scopes or []})  # fmt: skip
        if method == "GET" and path == f"/api/v1/trading/info/{env}/pnl":
            return _ok({"clientPortfolio": self._portfolio()})
        if method == "GET" and path == "/api/v1/market-data/exchanges":
            return _ok(
                {"exchangeInfo": [{"exchangeID": k, "exchangeDescription": v}
                                  for k, v in EXCHANGES.items()]}
            )  # fmt: skip
        if method == "GET" and path == "/api/v2/market-data/instruments":
            return self._instruments(q)
        if method == "GET" and path == "/api/v2/market-data/rates":
            ids = [int(x) for x in q.get("instrumentIds", "").split(",") if x]
            return _ok({"results": [
                {"instrumentId": i, "bid": self.instruments[i].bid, "ask": self.instruments[i].ask,
                 "date": NOW, "quoteType": "realtime"} for i in ids if i in self.instruments
            ]})  # fmt: skip
        if method == "POST" and path == f"/api/v2/trading/info/{'demo/' if env == 'demo' else ''}eligibility":
            return self._eligibility(body)
        hist = "/api/v1/trading/info/trade/" + ("demo/history" if env == "demo" else "history")
        if method == "GET" and path == hist:
            return self._history(q)
        if method == "POST" and path == f"{exe}/orders":
            return self._open(body, request_id)
        lookup = "/api/v2/trading/info/" + ("demo/" if env == "demo" else "") + "orders:lookup"
        if method == "GET" and path == lookup:
            return self._lookup(q)
        if method == "DELETE" and path.startswith(f"{exe}/orders/"):
            return self._cancel_open(int(path.rsplit("/", 1)[1]))
        if method == "POST" and path.startswith(f"{close}/market-close-orders/positions/"):
            return self._close(int(path.rsplit("/", 1)[1]), body, request_id)
        if method == "DELETE" and path.startswith(f"{close}/market-close-orders/"):
            return self._cancel_close(int(path.rsplit("/", 1)[1]))
        info = f"/api/v1/trading/info/{env}/close-orders/"
        if method == "GET" and path.startswith(info):
            order = self.close_orders.get(int(path.rsplit("/", 1)[1]))
            return _ok(self._close_info(order)) if order else _error(404, "NotFound", "no order")
        return _error(404, "NotFound", f"no route {method} {path}")

    def _instruments(self, q: dict[str, str]) -> httpx2.Response:
        if "instrumentsIds" in q:
            ids = {int(x) for x in q["instrumentsIds"].split(",") if x}
            found = [i for i in self.instruments.values() if i.id in ids]
        else:
            symbols = {s.upper() for s in q.get("symbols", "").split(",") if s}
            found = [i for i in self.instruments.values() if i.symbol.upper() in symbols]
        if not found:
            return _error(404, "NotFound", "No instrument found")
        return _ok(
            {"results": [{"instrumentId": i.id, "displayName": f"{i.symbol} Inc",
                          "type": i.type, "symbol": i.symbol, "exchangeId": i.exchange_id}
                         for i in found],
             "pagination": {"hasNext": False, "pageSize": 100}}
        )  # fmt: skip

    def _eligibility(self, body: dict[str, Any]) -> httpx2.Response:
        out = []
        for iid in body.get("instrumentIds") or []:
            i = self.instruments.get(int(iid))
            if i is None:
                continue
            out.append(
                {"instrumentId": i.id, "symbol": i.symbol, "minPositionExposure": i.min_exposure,
                 "maxUnitsPerOrder": i.max_units, "allowOpenPosition": i.allow_open,
                 "allowClosePosition": True, "allowPartialClosePosition": i.allow_partial_close,
                 "unitsQuantityType": i.units_type, "allowedOrderQuantityType": i.quantity_type,
                 "tradeUnitType": "units",
                 "leverageConfigs": [
                     {"settlementType": s, "direction": "long",
                      "leverageValues": [1] if s == "real" else [1, 2, 5]}
                     for s in i.settlements
                 ] + [{"settlementType": "cfd", "direction": "short", "leverageValues": [1, 2]}]}
            )  # fmt: skip
        return _ok({"currency": "USD", "eligibilities": out})

    def _history(self, q: dict[str, str]) -> httpx2.Response:
        if "minDate" not in q:
            return _error(400, "BadRequest", "minDate is required")
        since = q["minDate"][:10]
        page = int(q.get("page", "1"))
        size = int(q.get("pageSize", "100"))
        rows = [h for h in self.history if h["closeTimestamp"][:10] >= since]
        return _ok(rows[(page - 1) * size : page * size])

    def _open(self, body: dict[str, Any], request_id: str) -> httpx2.Response:
        for order in self.orders.values():
            if order["referenceId"] == request_id:  # idempotent by x-request-id
                return _ok({"token": "t", "orderId": order["orderId"], "referenceId": request_id})
        sizes = [k for k in ("amount", "units", "contracts") if body.get(k) is not None]
        if len(sizes) != 1 or body.get("action") != "open" or body.get("transaction") != "buy":
            return _error(400, "BadRequest", "invalid order")
        inst = self.instruments.get(int(body.get("instrumentId") or 0))
        if inst is None:
            return _error(400, "InstrumentNotFound", "unknown instrument")
        if int(body.get("leverage") or 1) != 1:
            return _error(400, "BadRequest", "leverage needs a stop loss")
        units = body.get("units")
        if units is None:
            units = float(body["amount"]) / inst.ask
        order_id = self._id()
        order = {
            "orderId": order_id, "referenceId": request_id, "instrumentId": inst.id,
            "symbol": inst.symbol, "settlementType": body.get("settlementType") or "real",
            "requestedUnits": float(units), "requestedAmount": body.get("amount"),
            "status": 1, "errorCode": 0, "errorMessage": "", "executions": [],
            "requestTime": NOW, "requestType": "byUnits" if body.get("units") else "byAmount",
        }  # fmt: skip
        self.orders[order_id] = order
        if self.fill_mode == "fill":
            self._execute_open(order, float(units))
        elif self.fill_mode == "partial":
            self._execute_open(order, float(units) / 2)
            order["status"] = 5
        elif self.fill_mode == "reject":
            order.update(status=4, errorCode=720, errorMessage="Insufficient funds")
        else:
            order["status"] = 11
        return _ok({"token": "t", "orderId": order_id, "referenceId": request_id})

    def _execute_open(self, order: dict[str, Any], units: float) -> None:
        inst = self.instruments[order["instrumentId"]]
        pid = self.add_position(inst.id, units, inst.ask, opened=NOW, order_id=order["orderId"])
        self.credit -= units * inst.ask
        order["executions"].append(
            {"positionId": pid, "state": "open", "remainingUnits": units,
             "openingData": {"openTime": NOW, "orderId": order["orderId"], "executionTime": NOW,
                             "units": units, "avgPrice": inst.ask, "fees": 0.0}}
        )  # fmt: skip
        order["status"] = 3

    def _lookup(self, q: dict[str, str]) -> httpx2.Response:
        order = None
        if "orderId" in q:
            order = self.orders.get(int(q["orderId"]))
        elif "referenceId" in q:
            order = next((o for o in self.orders.values() if o["referenceId"] == q["referenceId"]),
                         None)  # fmt: skip
        if order is None:
            return _error(404, "NotFound", "Order not found")
        names = {1: "Received", 2: "Placed", 3: "Filled", 4: "Rejected", 5: "PartiallyFilled",
                 7: "Canceled", 11: "WaitingForMarket"}  # fmt: skip
        return _ok(
            {"orderId": order["orderId"], "action": "open", "transaction": "buy", "type": "mkt",
             "status": {"id": order["status"], "name": names.get(order["status"], "?"),
                        "errorCode": order["errorCode"], "errorMessage": order["errorMessage"]},
             "asset": {"symbol": order["symbol"], "instrumentId": order["instrumentId"],
                       "currency": "USD", "settlementType": order["settlementType"],
                       "leverage": 1, "side": "long"},
             "requestedUnits": order["requestedUnits"],
             "positionExecutions": order["executions"], "requestTime": NOW, "lastUpdate": NOW,
             "requestType": order["requestType"]}
        )  # fmt: skip

    def _cancel_open(self, order_id: int) -> httpx2.Response:
        order = self.orders.get(order_id)
        if order is None:
            return _error(404, "NotFound", "Order not found")
        if order["status"] not in (1, 2, 11, 12):
            return _error(400, "BadRequest", "order is not pending")
        order["status"] = 7
        return _ok({"token": "t"})

    def _close(self, position_id: int, body: dict[str, Any], request_id: str) -> httpx2.Response:
        position = next((p for p in self.positions if p["positionID"] == position_id), None)
        if position is None:
            return _error(400, "PositionNotFound", "position not found")
        if int(body.get("InstrumentId") or 0) != position["instrumentID"]:
            return _error(400, "BadRequest", "instrument does not match the position")
        units = body.get("UnitsToDeduct")
        units = position["units"] if units is None else float(units)
        if units > position["units"] + 1e-9:
            return _error(400, "BadRequest", "more units than the position holds")
        order_id = self._id()
        order = {"orderID": order_id, "positionID": position_id, "units": units,
                 "instrumentID": position["instrumentID"], "referenceID": request_id,
                 "statusID": 1, "errorCode": 0, "errorMessage": "", "positions": []}  # fmt: skip
        self.close_orders[order_id] = order
        if self.fill_mode in ("fill", "partial"):
            self._execute_close(order)
        elif self.fill_mode == "reject":
            order.update(statusID=4, errorCode=741, errorMessage="Market closed")
        return _ok(
            {"orderForClose": {"positionID": position_id, "instrumentID": order["instrumentID"],
                               "unitsToDeduct": units, "orderID": order_id, "orderType": 19,
                               "statusID": order["statusID"], "CID": self.cid,
                               "openDateTime": NOW, "lastUpdate": NOW},
             "token": "t"}
        )  # fmt: skip

    def _execute_close(self, order: dict[str, Any]) -> None:
        position = next(p for p in self.positions if p["positionID"] == order["positionID"])
        inst = self.instruments[position["instrumentID"]]
        units = order["units"]
        position["units"] -= units
        if position["units"] <= 1e-9:
            self.positions.remove(position)
        self.credit += units * inst.bid
        order["statusID"] = 3
        order["positions"].append(
            {"positionID": position["positionID"], "occurred": NOW, "rate": inst.bid,
             "units": units, "conversionRate": 1.0, "amount": units * inst.bid}
        )  # fmt: skip
        self.history.append(
            {"positionId": position["positionID"], "instrumentId": inst.id, "isBuy": True,
             "leverage": 1, "openRate": position["openRate"],
             "openTimestamp": position["openDateTime"], "closeRate": inst.bid,
             "closeTimestamp": NOW, "units": units,
             "investment": units * position["openRate"],
             "netProfit": units * (inst.bid - position["openRate"]), "fees": 0.0,
             "orderId": position["orderID"]}
        )  # fmt: skip

    def _cancel_close(self, order_id: int) -> httpx2.Response:
        order = self.close_orders.get(order_id)
        if order is None or order["statusID"] != 1:
            return _error(400, "BadRequest", "order is not pending")
        order["statusID"] = 7
        return _ok({"token": "t"})

    def _close_info(self, order: dict[str, Any]) -> dict[str, Any]:
        return {"token": "t", "orderID": order["orderID"], "statusID": order["statusID"],
                "referenceID": order["referenceID"], "errorCode": order["errorCode"],
                "errorMessage": order["errorMessage"], "instrumentID": order["instrumentID"],
                "requestOccurred": NOW, "positions": list(order["positions"])}  # fmt: skip

    def _portfolio(self) -> dict[str, Any]:
        pending_open = [
            {"orderId": o["orderId"], "statusId": o["status"], "instrumentId": o["instrumentId"],
             "amount": o["requestedUnits"] * self.instruments[o["instrumentId"]].ask,
             "amountInUnits": o["requestedUnits"], "isBuy": True, "leverage": 1, "mirrorId": 0,
             "totalExternalCosts": 0.0, "openDateTime": NOW}
            for o in self.orders.values() if o["status"] in (1, 2, 11, 12)
        ]  # fmt: skip
        pending_close = [
            {"orderId": o["orderID"], "statusId": 1, "instrumentId": o["instrumentID"],
             "unitsToDeduct": o["units"], "positionId": o["positionID"], "openDateTime": NOW}
            for o in self.close_orders.values() if o["statusID"] == 1
        ]  # fmt: skip
        return {
            "positions": [dict(p) for p in self.positions],
            "credit": self.credit,
            "mirrors": list(self.mirrors),
            "orders": [],
            "ordersForOpen": pending_open,
            "ordersForClose": pending_close,
            "ordersForCloseMultiple": [],
            "bonusCredit": 0.0,
            "accountCurrencyId": 1,
        }

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def open_order_ids(self) -> list[int]:
        return [o["orderId"] for o in self.orders.values() if o["status"] in (1, 2, 11, 12)]


def _real_only(path: str) -> bool:
    return path in (
        "/api/v1/trading/info/trade/history",
        "/api/v2/trading/execution/orders",
        "/api/v2/trading/info/eligibility",
        "/api/v2/trading/info/orders:lookup",
    ) or path.startswith(
        (
            "/api/v2/trading/execution/orders/",
            "/api/v1/trading/execution/market-close-orders/",
        )
    )


def _ok(body: Any) -> httpx2.Response:
    return httpx2.Response(200, json=body)


def _error(status: int, code: str, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={"errorCode": code, "errorMessage": message})


def now() -> datetime:
    return datetime(2026, 3, 3, 16, 0, tzinfo=UTC)
