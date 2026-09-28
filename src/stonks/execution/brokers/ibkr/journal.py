"""The broker event journal and its replay (roadmap 23.15).

:class:`JournalingIbClient` wraps any :class:`IbClient` and writes one
:class:`JournalEvent` per call the broker makes to the gateway: its raw
answer (every value is one of the plain types of ``client.py``) or the
error it raised. Order answers are ``order_status`` events, executions are
``execution`` events, failed calls are ``error`` events and the rest are
``call`` events. An answer equal to the previous answer of the same call
is stored as ``unchanged`` without its body, so polling stays small.

:class:`FileJournal` appends each event as one JSON line to
``<dir>/<YYYY-MM-DD>.jsonl`` (UTC day of the event). :func:`read_journal`
reads files back and :class:`ReplayIbClient` serves them: each call gets
the next recorded answer of the same method, or raises the recorded
error. Drive a broker over a replay with the same calls as the incident
and it sees what it saw then. It never connects to anything.

Account ids never reach the disk: :func:`redact_account` keeps the letter
prefix (``DU`` for a paper account) and a short hash. The journal holds no
credentials (the gateway client has none).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.execution.brokers.ibkr import client as ib
from stonks.execution.brokers.ibkr.client import (
    IbAccountValue,
    IbApiError,
    IbConnectionError,
    IbContract,
    IbContractDetails,
    IbContractQuery,
    IbExecution,
    IbLinkStatus,
    IbOrderRequest,
    IbPosition,
    IbSnapshot,
    IbTrade,
    IbWhatIf,
)
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.ibkr.journal")

JournalKind = Literal["call", "order_status", "execution", "error"]

#: Calls whose answers are orders (``order_status`` events).
_ORDER_METHODS = frozenset({"place_order", "open_trades", "all_open_trades", "completed_trades"})
#: Account value tags whose value is the account id itself.
_ACCOUNT_TAGS = frozenset({"AccountCode", "AccountOrGroup"})
#: The plain types that may cross the journal, by name.
_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in vars(ib).values()
    if isinstance(cls, type) and dataclasses.is_dataclass(cls) and cls.__module__ == ib.__name__
}


# ---- events ------------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalEvent:
    seq: int
    #: When the call returned, ISO UTC.
    at: str
    kind: JournalKind
    method: str
    #: The call's arguments, encoded (account ids redacted).
    args: list[Any] = field(default_factory=list[Any])
    #: The answer, encoded; ``None`` for an error or an unchanged answer.
    result: Any = None
    #: The answer equals the previous answer of this method.
    unchanged: bool = False
    #: ``{"type", "message"}`` plus ``code`` and ``req_id`` for an IBKR error.
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournalEvent:
        return cls(
            seq=int(data["seq"]),
            at=str(data["at"]),
            kind=data["kind"],
            method=str(data["method"]),
            args=list(data.get("args") or []),
            result=data.get("result"),
            unchanged=bool(data.get("unchanged", False)),
            error=data.get("error"),
        )


class EventJournal:
    """Where events go. ``append`` must never raise into the broker."""

    def append(self, event: JournalEvent) -> None:
        raise NotImplementedError


class MemoryJournal(EventJournal):
    def __init__(self) -> None:
        self.events: list[JournalEvent] = []

    def append(self, event: JournalEvent) -> None:
        self.events.append(event)


class FileJournal(EventJournal):
    """One JSON line per event in ``<directory>/<UTC day>.jsonl``."""

    def __init__(self, directory: str | Path, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.directory = Path(directory)
        self._clock = clock
        self._lock = threading.Lock()

    def path_for(self, when: datetime) -> Path:
        return self.directory / f"{when.date().isoformat()}.jsonl"

    def append(self, event: JournalEvent) -> None:
        line = json.dumps(event.as_dict(), sort_keys=True, default=str)
        with self._lock:
            path = self.path_for(datetime.fromisoformat(event.at))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def read_journal(paths: Iterable[str | Path]) -> list[JournalEvent]:
    """Events of the files in order (a blank or broken line is skipped)."""
    events: list[JournalEvent] = []
    for path in paths:
        for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(JournalEvent.from_dict(json.loads(line)))
            except (ValueError, KeyError, TypeError):
                _log.warning("journal.bad_line", path=str(path), line=n)
    return events


# ---- encoding ----------------------------------------------------------------------------


_PREFIX = re.compile(r"[A-Za-z]*")


def redact_account(account: str) -> str:
    """``DU1234567`` to ``DU`` plus a short hash: stable, so a replay sees
    one account throughout, and a paper account still reads as paper."""
    if not account:
        return account
    prefix = _PREFIX.match(account)
    letters = prefix.group(0) if prefix else ""
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()[:8]
    return f"{letters}x{digest}"


def _redact(value: Any) -> Any:
    """Account ids out of a plain value (before it is encoded)."""
    if isinstance(value, IbAccountValue):
        redacted = dataclasses.replace(value, account=redact_account(value.account))
        if value.tag in _ACCOUNT_TAGS:
            redacted = dataclasses.replace(redacted, value=redact_account(value.value))
        return redacted
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        changes = {}
        for f in dataclasses.fields(value):
            current = getattr(value, f.name)
            if f.name == "account" and isinstance(current, str):
                changes[f.name] = redact_account(current)
        return dataclasses.replace(value, **changes) if changes else value
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    return value


def encode(value: Any) -> Any:
    """A plain value as JSON: dataclasses tagged by type name."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        body = {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {"__type__": type(value).__name__, **body}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    return value


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if "__tuple__" in value:
            return tuple(decode(v) for v in value["__tuple__"])
        name = value.get("__type__")
        if name is not None:
            cls = _TYPES.get(name)
            if cls is None:
                raise ValueError(f"unknown journal type {name!r}")
            fields = {k: decode(v) for k, v in value.items() if k != "__type__"}
            return cls(**fields)
        return {k: decode(v) for k, v in value.items()}
    return value


def _error_of(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, IbApiError):
        return {
            "type": "IbApiError",
            "code": exc.code,
            "message": exc.message,
            "req_id": exc.req_id,
        }
    return {"type": type(exc).__name__, "message": str(exc)}


def _raise(error: dict[str, Any]) -> None:
    kind = error.get("type")
    message = str(error.get("message", ""))
    if kind == "IbApiError":
        raise IbApiError(int(error.get("code", -1)), message, req_id=int(error.get("req_id", -1)))
    if kind == "IbConnectionError":
        raise IbConnectionError(message)
    if kind == "TimeoutError":
        raise TimeoutError(message)
    raise RuntimeError(f"{kind}: {message}")


def _kind(method: str) -> JournalKind:
    if method in _ORDER_METHODS:
        return "order_status"
    if method == "executions":
        return "execution"
    return "call"


# ---- recording ---------------------------------------------------------------------------


class JournalingIbClient:
    """An :class:`IbClient` that writes every call to a journal. Optional
    calls (shortability, option data and events) pass through when the
    wrapped client has them."""

    def __init__(self, inner: Any, journal: EventJournal, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._inner = inner
        self._journal = journal
        self._clock = clock
        self._seq = 0
        self._last: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def inner(self) -> Any:
        return self._inner

    def _call(self, method: str, *args: Any) -> Any:
        fn: Callable[..., Any] = getattr(self._inner, method)
        try:
            result = fn(*args)
        except Exception as exc:
            self._record(method, args, error=_error_of(exc))
            raise
        self._record(method, args, result=result)
        return result

    def _record(
        self,
        method: str,
        args: Sequence[Any],
        *,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        try:
            encoded_args = [encode(_redact(a)) for a in args]
            if method in ("positions", "account_values"):
                encoded_args = [redact_account(a) if isinstance(a, str) else a for a in args]
            with self._lock:
                self._seq += 1
                seq = self._seq
                if error is not None:
                    event = JournalEvent(
                        seq, self._at(), "error", method, encoded_args, error=error
                    )
                else:
                    body = encode(_redact(_listed(result)))
                    if method == "managed_accounts":
                        body = [redact_account(a) for a in result]
                    unchanged = method in self._last and self._last[method] == body
                    self._last[method] = body
                    event = JournalEvent(
                        seq,
                        self._at(),
                        _kind(method),
                        method,
                        encoded_args,
                        result=None if unchanged else body,
                        unchanged=unchanged,
                    )
            self._journal.append(event)
        except Exception as exc:  # the journal never breaks trading
            _log.error("journal.write_failed", method=method, error=str(exc))

    def _at(self) -> str:
        return self._clock.now().isoformat()

    # ---- IbClient ---------------------------------------------------------------------

    def connect(self) -> None:
        self._call("connect")

    def disconnect(self) -> None:
        self._call("disconnect")

    def status(self) -> IbLinkStatus:
        return self._call("status")

    def managed_accounts(self) -> Sequence[str]:
        return self._call("managed_accounts")

    def server_time(self) -> datetime:
        return self._call("server_time")

    def contract_details(self, query: IbContractQuery) -> Sequence[IbContractDetails]:
        return self._call("contract_details", query)

    def place_order(self, contract: IbContract, order: IbOrderRequest) -> IbTrade:
        return self._call("place_order", contract, order)

    def cancel_order(self, order_id: int) -> None:
        self._call("cancel_order", order_id)

    def global_cancel(self) -> None:
        self._call("global_cancel")

    def open_trades(self) -> Sequence[IbTrade]:
        return self._call("open_trades")

    def all_open_trades(self) -> Sequence[IbTrade]:
        return self._call("all_open_trades")

    def completed_trades(self) -> Sequence[IbTrade]:
        return self._call("completed_trades")

    def executions(self) -> Sequence[IbExecution]:
        return self._call("executions")

    def positions(self, account: str) -> Sequence[IbPosition]:
        return self._call("positions", account)

    def account_values(self, account: str) -> Sequence[IbAccountValue]:
        return self._call("account_values", account)

    def what_if(self, contract: IbContract, order: IbOrderRequest) -> IbWhatIf:
        return self._call("what_if", contract, order)

    def snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbSnapshot]:
        return self._call("snapshots", list(contracts))

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()

    def __getattr__(self, name: str) -> Any:
        """The optional calls (``shortability``, ``option_params``, ...),
        recorded too, only when the wrapped client has them."""
        if name.startswith("_") or name not in _OPTIONAL:
            raise AttributeError(name)
        getattr(self._inner, name)  # AttributeError when the client lacks it
        return lambda *args: self._call(name, *args)


#: Optional client calls (``IbShortableClient``, the option protocols).
_OPTIONAL = frozenset({"shortability", "option_params", "option_snapshots", "option_events"})


def _listed(value: Any) -> Any:
    """Sequences as lists so a tuple and a list answer compare equal."""
    if isinstance(value, tuple) and not dataclasses.is_dataclass(value):
        return list(value)
    return value


# ---- replay ------------------------------------------------------------------------------


class ReplayIbClient:
    """Answers each call with the next recorded answer of the same method.
    A method with nothing left raises :class:`IbConnectionError`."""

    def __init__(self, events: Iterable[JournalEvent]) -> None:
        self._queues: dict[str, deque[JournalEvent]] = defaultdict(deque)
        self._last: dict[str, Any] = {}
        for event in sorted(events, key=lambda e: e.seq):
            self._queues[event.method].append(event)

    @classmethod
    def from_files(cls, paths: Iterable[str | Path]) -> ReplayIbClient:
        return cls(read_journal(paths))

    def remaining(self) -> dict[str, int]:
        """Recorded calls not replayed yet, per method."""
        return {m: len(q) for m, q in self._queues.items() if q}

    def _next(self, method: str) -> Any:
        queue = self._queues.get(method)
        if not queue:
            raise IbConnectionError(f"the journal has no more {method} calls")
        event = queue.popleft()
        if event.error is not None:
            _raise(event.error)
        if event.unchanged:
            return decode(self._last.get(method))
        self._last[method] = event.result
        return decode(event.result)

    def connect(self) -> None:
        self._next("connect")

    def disconnect(self) -> None:
        if self._queues.get("disconnect"):
            self._next("disconnect")

    def status(self) -> IbLinkStatus:
        return self._next("status")

    def managed_accounts(self) -> Sequence[str]:
        return self._next("managed_accounts")

    def server_time(self) -> datetime:
        return self._next("server_time")

    def contract_details(self, query: IbContractQuery) -> Sequence[IbContractDetails]:
        return self._next("contract_details")

    def place_order(self, contract: IbContract, order: IbOrderRequest) -> IbTrade:
        return self._next("place_order")

    def cancel_order(self, order_id: int) -> None:
        self._next("cancel_order")

    def global_cancel(self) -> None:
        self._next("global_cancel")

    def open_trades(self) -> Sequence[IbTrade]:
        return self._next("open_trades")

    def all_open_trades(self) -> Sequence[IbTrade]:
        return self._next("all_open_trades")

    def completed_trades(self) -> Sequence[IbTrade]:
        return self._next("completed_trades")

    def executions(self) -> Sequence[IbExecution]:
        return self._next("executions")

    def positions(self, account: str) -> Sequence[IbPosition]:
        return self._next("positions")

    def account_values(self, account: str) -> Sequence[IbAccountValue]:
        return self._next("account_values")

    def what_if(self, contract: IbContract, order: IbOrderRequest) -> IbWhatIf:
        return self._next("what_if")

    def snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbSnapshot]:
        return self._next("snapshots")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in _OPTIONAL or not self._queues.get(name):
            raise AttributeError(name)
        return lambda *args: self._next(name)


def journal_summary(events: Sequence[JournalEvent]) -> list[dict[str, Any]]:
    """One row per event for a person: time, kind, call and what changed
    (an order's ref, status and filled quantity, an execution, an error)."""
    rows: list[dict[str, Any]] = []
    for e in events:
        detail = ""
        if e.error is not None:
            code = e.error.get("code")
            detail = f"{e.error.get('type')}{f' {code}' if code is not None else ''}: {e.error.get('message')}"
        elif e.unchanged:
            detail = "unchanged"
        elif e.kind == "order_status":
            trades = e.result if isinstance(e.result, list) else [e.result]
            detail = "; ".join(
                f"{t.get('order_ref')} {t.get('status')} {t.get('filled')}/{t.get('total_quantity')}"
                for t in trades
                if isinstance(t, dict)
            )
        elif e.kind == "execution" and isinstance(e.result, list):
            detail = "; ".join(
                f"{x.get('exec_id')} {x.get('side')} {x.get('shares')} @ {x.get('price')}"
                for x in e.result
                if isinstance(x, dict)
            )
        rows.append(
            {"seq": e.seq, "at": e.at, "kind": e.kind, "method": e.method, "detail": detail}
        )
    return rows
