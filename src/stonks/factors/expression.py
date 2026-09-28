"""The factor expression language (roadmap 22.2), modelled on Qlib's.

A factor is a formula over the bar fields ``$open``, ``$high``, ``$low``,
``$close`` and ``$volume``, for example ``Mean($close, 20) / $close``.
Operators (see :func:`operators`):

- arithmetic ``+ - * /``, comparisons ``> >= < <= == !=`` (1.0 or 0.0),
  ``&`` and ``|`` (logical and, or), and unary minus;
- per row: ``Log``, ``Abs``, ``Sign``, ``If(cond, a, b)``, ``Greater(a, b)``
  and ``Less(a, b)`` (the larger or smaller of two values);
- rolling over each ticker's last ``N`` bars: ``Ref(x, N)`` (the value
  ``N`` bars ago), ``Delta(x, N)``, ``Mean``, ``Std``, ``Sum``, ``Min``,
  ``Max``, ``Rank`` (percentile of today's value in the window),
  ``Quantile(x, N, q)``, ``Slope``, ``Rsquare``, ``Resi`` (a regression on
  time), ``IdxMax``, ``IdxMin`` and ``Corr(x, y, N)``;
- across tickers on each date: ``CSRank(x)`` (percentile rank, 0 to 1).

Text is parsed with Python's own parser into a small tree of
:class:`Field`, :class:`Const` and :class:`Call` nodes, and only the names
above are accepted, so an expression can never run code. ``str(node)`` is
the canonical form (``Div(Mean($close,20),$close)``), used as a cache key.

Point in time (P12):

- **No look-ahead.** A negative ``Ref`` offset reads later bars. Only the
  evaluation label (:func:`next_open_label`) may do that; a factor with one
  is refused (:func:`parse_factor`).
- **Adjusted prices, as of the date.** Panels read split- and
  dividend-adjusted bars. The level of an adjusted price depends on actions
  after the date, so the compiler evaluates in fully adjusted units and
  rescales the result to the units known on the date. That is exact when
  the formula scales cleanly with prices and volumes, which :func:`dims`
  checks (a ratio such as ``$close / Ref($close, 5)``, a level such as
  ``Mean($close, 5)``, a correlation). A formula that mixes levels with
  constants or other levels (``$close > 10``, ``$close + $volume``) would
  depend on later splits, so it is refused with a hint to use a ratio.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "DIMLESS",
    "FIELDS",
    "Call",
    "Const",
    "Dim",
    "ExpressionError",
    "Field",
    "Node",
    "OpSpec",
    "dims",
    "lead",
    "lookback",
    "next_open_label",
    "operators",
    "parse",
    "parse_factor",
]

#: Bar fields and their (price, volume) dimension.
FIELDS: dict[str, tuple[int, int]] = {
    "open": (1, 0),
    "high": (1, 0),
    "low": (1, 0),
    "close": (1, 0),
    "volume": (0, 1),
}

MAX_LENGTH = 4000
MAX_NODES = 250
MAX_WINDOW = 5000


class ExpressionError(ValueError):
    """An expression that cannot be parsed or is not point in time."""


# ---- the tree -------------------------------------------------------------------------


def _num(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


@dataclass(frozen=True)
class Field:
    name: str

    def __str__(self) -> str:
        return f"${self.name}"


@dataclass(frozen=True)
class Const:
    value: float

    def __str__(self) -> str:
        return _num(self.value)


@dataclass(frozen=True)
class Call:
    op: str
    args: tuple[Node, ...]

    def __str__(self) -> str:
        return f"{self.op}({','.join(str(a) for a in self.args)})"

    @property
    def spec(self) -> OpSpec:
        return OPS[self.op]

    @property
    def series(self) -> tuple[Node, ...]:
        """The series arguments (the rest are numeric parameters)."""
        return self.args[: self.spec.n_series]

    @property
    def params(self) -> tuple[float, ...]:
        out: list[float] = []
        for a in self.args[self.spec.n_series :]:
            assert isinstance(a, Const)
            out.append(a.value)
        return tuple(out)


Node = Field | Const | Call

Kind = Literal["scalar", "rolling", "cross"]


@dataclass(frozen=True)
class OpSpec:
    name: str
    kind: Kind
    #: Series arguments, then ``n_params`` numeric ones (window, quantile).
    n_series: int
    n_params: int
    summary: str
    #: Fewest bars in a rolling window.
    min_window: int = 1

    def to_dict(self) -> dict[str, Any]:
        params = ["x", "y", "z"][: self.n_series]
        if self.kind == "rolling":
            params.append("N")
            if self.n_params == 2:
                params.append("q")
        return {
            "name": self.name,
            "kind": self.kind,
            "signature": f"{self.name}({', '.join(params)})",
            "summary": self.summary,
        }


def _ops(*specs: OpSpec) -> dict[str, OpSpec]:
    return {s.name: s for s in specs}


OPS: dict[str, OpSpec] = _ops(
    OpSpec("Add", "scalar", 2, 0, "x + y"),
    OpSpec("Sub", "scalar", 2, 0, "x - y"),
    OpSpec("Mul", "scalar", 2, 0, "x * y"),
    OpSpec("Div", "scalar", 2, 0, "x / y (empty when y is 0)"),
    OpSpec("Neg", "scalar", 1, 0, "-x"),
    OpSpec("Gt", "scalar", 2, 0, "x > y as 1 or 0"),
    OpSpec("Ge", "scalar", 2, 0, "x >= y as 1 or 0"),
    OpSpec("Lt", "scalar", 2, 0, "x < y as 1 or 0"),
    OpSpec("Le", "scalar", 2, 0, "x <= y as 1 or 0"),
    OpSpec("Eq", "scalar", 2, 0, "x == y as 1 or 0"),
    OpSpec("Ne", "scalar", 2, 0, "x != y as 1 or 0"),
    OpSpec("And", "scalar", 2, 0, "x & y: 1 when both are non-zero"),
    OpSpec("Or", "scalar", 2, 0, "x | y: 1 when either is non-zero"),
    OpSpec("Log", "scalar", 1, 0, "natural log (empty when x <= 0)"),
    OpSpec("Abs", "scalar", 1, 0, "absolute value"),
    OpSpec("Sign", "scalar", 1, 0, "-1, 0 or 1"),
    OpSpec("If", "scalar", 3, 0, "y when x is non-zero, else z"),
    OpSpec("Greater", "scalar", 2, 0, "the larger of x and y"),
    OpSpec("Less", "scalar", 2, 0, "the smaller of x and y"),
    OpSpec("Ref", "rolling", 1, 1, "x N bars ago (a negative N reads the future: label only)"),
    OpSpec("Delta", "rolling", 1, 1, "x minus x N bars ago"),
    OpSpec("Mean", "rolling", 1, 1, "mean of the last N bars"),
    OpSpec("Std", "rolling", 1, 1, "sample standard deviation of the last N bars", 2),
    OpSpec("Sum", "rolling", 1, 1, "sum of the last N bars"),
    OpSpec("Min", "rolling", 1, 1, "lowest of the last N bars"),
    OpSpec("Max", "rolling", 1, 1, "highest of the last N bars"),
    OpSpec("Rank", "rolling", 1, 1, "percentile of today's value among the last N bars"),
    OpSpec("Quantile", "rolling", 1, 2, "the q quantile of the last N bars"),
    OpSpec("Slope", "rolling", 1, 1, "slope of a line fitted to the last N bars", 2),
    OpSpec("Rsquare", "rolling", 1, 1, "R squared of that line", 2),
    OpSpec("Resi", "rolling", 1, 1, "today's distance from that line", 2),
    OpSpec("IdxMax", "rolling", 1, 1, "position of the highest of the last N bars (1 = oldest)"),
    OpSpec("IdxMin", "rolling", 1, 1, "position of the lowest of the last N bars (1 = oldest)"),
    OpSpec("Corr", "rolling", 2, 1, "correlation of x and y over the last N bars", 2),
    OpSpec("CSRank", "cross", 1, 0, "percentile rank across tickers on each date"),
)


def operators() -> list[dict[str, Any]]:
    """Every operator a formula may call (the infix ones included)."""
    return [spec.to_dict() for spec in OPS.values()]


# ---- parsing --------------------------------------------------------------------------

_FIELD_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_FIELD_PREFIX = "__field_"

_BINARY: dict[type, str] = {
    ast.Add: "Add",
    ast.Sub: "Sub",
    ast.Mult: "Mul",
    ast.Div: "Div",
    ast.BitAnd: "And",
    ast.BitOr: "Or",
}
_COMPARE: dict[type, str] = {
    ast.Gt: "Gt",
    ast.GtE: "Ge",
    ast.Lt: "Lt",
    ast.LtE: "Le",
    ast.Eq: "Eq",
    ast.NotEq: "Ne",
}


def parse(text: str) -> Node:
    """The tree of ``text``. Raises :class:`ExpressionError` on anything
    outside the language. Future reads are allowed here (the label needs
    them); a factor goes through :func:`parse_factor`."""
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("the expression is empty")
    if len(text) > MAX_LENGTH:
        raise ExpressionError(f"the expression is too long (at most {MAX_LENGTH} characters)")
    source = _FIELD_RE.sub(lambda m: _FIELD_PREFIX + m.group(1), text.strip())
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error at column {exc.offset}: {exc.msg}") from None
    except (ValueError, RecursionError, MemoryError):
        raise ExpressionError("the expression has too many parts") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES * 3:
        raise ExpressionError(f"the expression has too many parts (at most {MAX_NODES})")
    counter = [0]
    return _build(tree.body, counter)


def parse_factor(text: str) -> Node:
    """:func:`parse` for a factor: refuses future reads and formulas that
    are not point in time once prices are adjusted (module doc)."""
    node = parse(text)
    check_factor(node)
    return node


def check_factor(node: Node) -> None:
    if lead(node) > 0:
        raise ExpressionError(
            "a negative Ref offset reads the future; only the evaluation label may do that"
        )
    if dims(node).kind == "mixed":
        raise ExpressionError(
            "this formula compares or mixes a price or volume level with something of "
            "another scale, so its value would change with later splits and dividends; "
            "use a ratio such as $close / Ref($close, 20) or divide by $close or $volume"
        )


def next_open_label(horizon: int) -> Node:
    """The forward return from the next open over ``horizon`` bars,
    ``Ref($open, -(h+1)) / Ref($open, -1) - 1``: the score at ``t`` sees
    ``t``'s close and trades at the next open (the signal_eval convention).
    For evaluation only."""
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1 bar, got {horizon}")
    return Call(
        "Sub",
        (
            Call(
                "Div",
                (
                    Call("Ref", (Field("open"), Const(float(-(horizon + 1))))),
                    Call("Ref", (Field("open"), Const(-1.0))),
                ),
            ),
            Const(1.0),
        ),
    )


def _count(counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise ExpressionError(f"the expression has too many parts (at most {MAX_NODES})")


def _build(node: ast.AST, counter: list[int]) -> Node:
    _count(counter)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ExpressionError(f"a constant must be a number, got {node.value!r}")
        return Const(float(node.value))
    if isinstance(node, ast.Name):
        if node.id.startswith(_FIELD_PREFIX):
            name = node.id[len(_FIELD_PREFIX) :]
            if name not in FIELDS:
                raise ExpressionError(
                    f"unknown field ${name}; use one of {', '.join('$' + f for f in FIELDS)}"
                )
            return Field(name)
        raise ExpressionError(f"unknown name {node.id!r}; fields start with $, e.g. $close")
    if isinstance(node, ast.BinOp):
        op = _BINARY.get(type(node.op))
        if op is None:
            raise ExpressionError(f"operator {type(node.op).__name__} is not supported")
        return Call(op, (_build(node.left, counter), _build(node.right, counter)))
    if isinstance(node, ast.UnaryOp):
        inner = _build(node.operand, counter)
        if isinstance(node.op, ast.UAdd):
            return inner
        if isinstance(node.op, ast.USub):
            return Const(-inner.value) if isinstance(inner, Const) else Call("Neg", (inner,))
        raise ExpressionError(f"operator {type(node.op).__name__} is not supported")
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ExpressionError("chained comparisons are not supported; use &")
        op = _COMPARE.get(type(node.ops[0]))
        if op is None:
            raise ExpressionError(f"comparison {type(node.ops[0]).__name__} is not supported")
        return Call(op, (_build(node.left, counter), _build(node.comparators[0], counter)))
    if isinstance(node, ast.BoolOp):
        op = "And" if isinstance(node.op, ast.And) else "Or"
        values = [_build(v, counter) for v in node.values]
        out = values[0]
        for v in values[1:]:
            out = Call(op, (out, v))
        return out
    if isinstance(node, ast.Call):
        return _build_call(node, counter)
    raise ExpressionError(f"{type(node).__name__} is not supported in an expression")


def _build_call(node: ast.Call, counter: list[int]) -> Node:
    name = node.func.id if isinstance(node.func, ast.Name) else None
    spec = OPS.get(name or "")
    if spec is None or name is None:
        shown = name or ast.unparse(node.func)
        raise ExpressionError(f"unknown operator {shown!r}; see the operator list")
    if node.keywords:
        raise ExpressionError(f"{name} takes no keyword arguments")
    want = spec.n_series + spec.n_params
    if len(node.args) != want:
        raise ExpressionError(f"{name} takes {want} arguments, got {len(node.args)}")
    series = tuple(_build(a, counter) for a in node.args[: spec.n_series])
    params = tuple(_param(spec, i, a) for i, a in enumerate(node.args[spec.n_series :]))
    return Call(name, series + params)


def _literal(node: ast.AST) -> float | None:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal(node.operand)
        return None if inner is None else -inner
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    return None


def _param(spec: OpSpec, index: int, node: ast.AST) -> Const:
    value = _literal(node)
    if index == 1:  # Quantile's q
        if value is None or not 0.0 < value < 1.0:
            raise ExpressionError(f"{spec.name}'s q must be a number between 0 and 1")
        return Const(value)
    if value is None or not math.isfinite(value) or value != int(value):
        raise ExpressionError(f"{spec.name}'s window must be a whole number")
    n = int(value)
    if abs(n) > MAX_WINDOW:
        raise ExpressionError(f"{spec.name}'s window must be at most {MAX_WINDOW} bars")
    if spec.name == "Ref":
        return Const(float(n))
    if n < spec.min_window:
        raise ExpressionError(f"{spec.name}'s window must be at least {spec.min_window}")
    return Const(float(n))


# ---- analysis -------------------------------------------------------------------------


def lookback(node: Node) -> int:
    """Bars before the date the formula reads (its warm-up)."""
    if not isinstance(node, Call):
        return 0
    inner = max((lookback(a) for a in node.series), default=0)
    if node.spec.kind != "rolling":
        return inner
    n = int(node.params[0])
    if node.op in ("Ref", "Delta"):
        return inner + max(n, 0)
    return inner + n - 1


def lead(node: Node) -> int:
    """Bars after the date the formula reads (0 for a factor)."""
    if not isinstance(node, Call):
        return 0
    inner = max((lead(a) for a in node.series), default=0)
    if node.op == "Ref" and node.params[0] < 0:
        return inner + int(-node.params[0])
    return inner


@dataclass(frozen=True)
class Dim:
    """How a value scales when prices are multiplied by ``F`` and volumes
    by ``G``: ``pow`` values by ``F**p * G**v``, ``log`` values shift by
    ``p*log F + v*log G``. ``any`` is a constant, ``mixed`` scales in no
    clean way (not point in time once prices are adjusted)."""

    kind: Literal["any", "pow", "log", "mixed"]
    p: float = 0.0
    v: float = 0.0

    @property
    def is_scaled(self) -> bool:
        """True when the value needs rescaling to the date's units."""
        return self.kind in ("pow", "log") and (self.p != 0 or self.v != 0)


ANY = Dim("any")
DIMLESS = Dim("pow", 0.0, 0.0)
MIXED = Dim("mixed")


def _log(p: float, v: float) -> Dim:
    return DIMLESS if p == 0 and v == 0 else Dim("log", p, v)


def _is_small_const(node: Node) -> bool:
    return isinstance(node, Const) and abs(node.value) < 1e-6


def _add(a: Dim, b: Dim, sign: float, a_node: Node, b_node: Node) -> Dim:
    """Sum or difference. A constant added to a price or volume level is
    refused unless it is tiny (an epsilon against division by zero): its
    size would depend on later splits."""
    if a.kind == "mixed" or b.kind == "mixed":
        return MIXED
    if b.kind == "any":
        return a if (not a.is_scaled or _is_small_const(b_node)) else MIXED
    if a.kind == "any":
        if b.is_scaled and not _is_small_const(a_node):
            return MIXED
        return b if sign > 0 or b.kind != "log" else _log(-b.p, -b.v)
    if a.kind == "pow" and b.kind == "pow":
        return a if a == b else MIXED
    if a.kind == "log" and b.kind == "log":
        return _log(a.p + sign * b.p, a.v + sign * b.v)
    if a.kind == "log" and b == DIMLESS:
        return a
    if b.kind == "log" and a == DIMLESS:
        return b if sign > 0 else _log(-b.p, -b.v)
    return MIXED


def _same(a: Dim, b: Dim, a_node: Node, b_node: Node) -> Dim:
    """The dimension of an operator that needs both sides in one scale
    (comparisons, Greater, Less, If branches). A constant fits only a
    dimensionless side, or any side when it is zero."""
    if a.kind == "mixed" or b.kind == "mixed":
        return MIXED
    if a.kind == "any" and b.kind == "any":
        return ANY
    if b.kind == "any":
        return a if (a == DIMLESS or _is_small_const(b_node)) else MIXED
    if a.kind == "any":
        return b if (b == DIMLESS or _is_small_const(a_node)) else MIXED
    return a if a == b else MIXED


def _const(node: Node) -> float | None:
    return node.value if isinstance(node, Const) else None


def dims(node: Node) -> Dim:
    """The :class:`Dim` of a formula (module doc)."""
    if isinstance(node, Const):
        return ANY
    if isinstance(node, Field):
        p, v = FIELDS[node.name]
        return Dim("pow", float(p), float(v))
    op = node.op
    ds = [dims(a) for a in node.series]
    if any(d.kind == "mixed" for d in ds):
        return MIXED
    if op in ("Add", "Sub"):
        return _add(ds[0], ds[1], 1.0 if op == "Add" else -1.0, *node.series)
    if op in ("Mul", "Div"):
        return _mul(op, ds[0], ds[1], node.series)
    if op == "Neg":
        d = ds[0]
        return _log(-d.p, -d.v) if d.kind == "log" else d
    if op in ("Gt", "Ge", "Lt", "Le", "Eq", "Ne"):
        same = _same(ds[0], ds[1], node.series[0], node.series[1])
        return MIXED if same.kind == "mixed" else DIMLESS
    if op in ("And", "Or"):
        return DIMLESS if all(d in (DIMLESS, ANY) for d in ds) else MIXED
    if op in ("Greater", "Less"):
        return _same(ds[0], ds[1], node.series[0], node.series[1])
    if op == "If":
        if ds[0] not in (DIMLESS, ANY):
            return MIXED
        return _same(ds[1], ds[2], node.series[1], node.series[2])
    if op == "Log":
        d = ds[0]
        if d.kind == "any":
            return ANY
        return _log(d.p, d.v) if d.kind == "pow" else MIXED
    if op == "Abs":
        return ds[0] if ds[0].kind in ("pow", "any") else MIXED
    if op == "Sign":
        d = ds[0]
        return ANY if d.kind == "any" else (DIMLESS if d.kind == "pow" else MIXED)
    return _rolling_dims(node, ds)


def _mul(op: str, a: Dim, b: Dim, series: tuple[Node, ...]) -> Dim:
    if a.kind == "any" and b.kind == "any":
        return ANY
    if a.kind == "pow" and b.kind == "pow":
        s = 1.0 if op == "Mul" else -1.0
        return Dim("pow", a.p + s * b.p, a.v + s * b.v)
    if b.kind == "any":
        c = _const(series[1])
        if a.kind == "pow":
            return a
        if c:  # a log value times or over a constant
            k = c if op == "Mul" else 1.0 / c
            return _log(a.p * k, a.v * k)
        return MIXED
    if a.kind == "any":
        c = _const(series[0])
        if b.kind == "pow":
            return b if op == "Mul" else Dim("pow", -b.p, -b.v)
        if op == "Mul" and c:
            return _log(b.p * c, b.v * c)
        return MIXED
    return MIXED


def _rolling_dims(node: Call, ds: list[Dim]) -> Dim:
    op, d = node.op, ds[0]
    if op == "CSRank":
        return ANY if d.kind == "any" else DIMLESS
    if op in ("Rank", "Rsquare", "IdxMax", "IdxMin", "Corr"):
        return ANY if all(x.kind == "any" for x in ds) else DIMLESS
    if d.kind == "any":
        return ANY
    if op in ("Ref", "Mean", "Min", "Max", "Quantile"):
        return d
    if op == "Sum":
        n = node.params[0]
        return _log(d.p * n, d.v * n) if d.kind == "log" else d
    if op in ("Delta", "Std", "Resi", "Slope"):
        return DIMLESS if d.kind == "log" else d
    raise AssertionError(f"no dimension rule for {op}")  # pragma: no cover
