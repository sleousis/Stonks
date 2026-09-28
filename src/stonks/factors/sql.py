"""Compile a factor expression to DuckDB SQL (roadmap 22.2).

The input relation holds one row per ticker and bar: ``ticker``,
``timestamp``, the fields ``open``, ``high``, ``low``, ``close`` and
``volume`` in *fully adjusted* units, the adjustment factors ``pf``
(prices) and ``vf`` (volumes) that turned raw bars into those units, and
``member`` (the ticker was in the universe on that date). See
:mod:`stonks.factors.engine` for how it is built.

Rolling operators are window functions over each ticker's rows ordered by
time; ``CSRank`` is a window over each date's rows. DuckDB cannot nest
window functions, so every window becomes a column of its own layer:
``t1 = SELECT *, <windows over t0> FROM t0``, then ``t2`` over ``t1``, and
so on. Identical sub-expressions share one column.

Rules the SQL follows:

- a rolling window needs ``N`` non-empty values, else the result is empty
  (no partial warm-up values);
- division by zero, the log of a non-positive value and a non-finite
  result are empty (NULL), never infinite;
- ``CSRank`` ranks only members with a value (average rank / count, as
  pandas ``rank(pct=True)``);
- the result, and every ``CSRank`` input, is rescaled from fully adjusted
  units to the units known on its own date using its dimension
  (:func:`stonks.factors.expression.dims`), so a later split or dividend
  never changes an earlier value.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stonks.factors.expression import Call, Const, Dim, Field, Node, dims

__all__ = ["compile_sql"]

_ORDER = "PARTITION BY ticker ORDER BY timestamp"


def _lit(value: float) -> str:
    return f"CAST({float(value)!r} AS DOUBLE)"


def _rescale(sql: str, dim: Dim) -> str:
    """``sql`` (fully adjusted units) in the units known on its row's date."""
    if not dim.is_scaled:
        return sql
    if dim.kind == "pow":
        terms = []
        if dim.p:
            terms.append(f"POWER(pf, {float(dim.p)!r})")
        if dim.v:
            terms.append(f"POWER(vf, {float(dim.v)!r})")
        return f"({sql} / ({' * '.join(terms)}))"
    terms = []
    if dim.p:
        terms.append(f"{float(dim.p)!r} * LN(pf)")
    if dim.v:
        terms.append(f"{float(dim.v)!r} * LN(vf)")
    return f"({sql} - ({' + '.join(terms)}))"


@dataclass
class _Compiler:
    #: ``layers[k]`` holds the ``(column, sql)`` pairs computed over ``t{k}``.
    layers: list[list[tuple[str, str]]] = field(default_factory=list)
    memo: dict[str, tuple[str, int]] = field(default_factory=dict)

    def column(self, sql: str, level: int) -> tuple[str, int]:
        """A new column holding ``sql`` (a window over ``t{level}``)."""
        while len(self.layers) <= level:
            self.layers.append([])
        name = f"_c{sum(len(layer) for layer in self.layers) + 1}"
        self.layers[level].append((name, sql))
        return name, level + 1

    def compile(self, node: Node) -> tuple[str, int]:
        """``(sql, level)``: ``sql`` reads only columns of ``t{level}``."""
        key = str(node)
        if key not in self.memo:
            self.memo[key] = self._compile(node)
        return self.memo[key]

    def _compile(self, node: Node) -> tuple[str, int]:
        if isinstance(node, Const):
            return _lit(node.value), 0
        if isinstance(node, Field):
            return node.name, 0
        kind = node.spec.kind
        if kind == "scalar":
            parts = [self.compile(a) for a in node.series]
            return _scalar(node.op, [p[0] for p in parts]), max(p[1] for p in parts)
        if kind == "cross":
            arg = node.series[0]
            sql, level = self.compile(arg)
            return self.column(_csrank(_rescale(sql, dims(arg))), level)
        parts = [self.compile(a) for a in node.series]
        level = max(p[1] for p in parts)
        return self.column(_rolling(node, [p[0] for p in parts]), level)


def _scalar(op: str, a: list[str]) -> str:
    x = a[0]
    y = a[1] if len(a) > 1 else ""
    if op == "Add":
        return f"({x} + {y})"
    if op == "Sub":
        return f"({x} - {y})"
    if op == "Mul":
        return f"({x} * {y})"
    if op == "Div":
        return f"({x} / NULLIF({y}, 0))"
    if op == "Neg":
        return f"(-{x})"
    compare = {"Gt": ">", "Ge": ">=", "Lt": "<", "Le": "<=", "Eq": "=", "Ne": "<>"}
    if op in compare:
        return f"CAST(({x} {compare[op]} {y}) AS DOUBLE)"
    if op == "And":
        return f"CAST((({x}) <> 0 AND ({y}) <> 0) AS DOUBLE)"
    if op == "Or":
        return f"CAST((({x}) <> 0 OR ({y}) <> 0) AS DOUBLE)"
    if op == "Log":
        return f"(CASE WHEN {x} > 0 THEN LN({x}) END)"
    if op == "Abs":
        return f"ABS({x})"
    if op == "Sign":
        return f"CAST(SIGN({x}) AS DOUBLE)"
    if op == "If":
        return f"(CASE WHEN ({x}) IS NULL THEN NULL WHEN ({x}) <> 0 THEN {y} ELSE {a[2]} END)"
    if op in ("Greater", "Less"):
        fn = "GREATEST" if op == "Greater" else "LEAST"
        return f"(CASE WHEN {x} IS NULL OR {y} IS NULL THEN NULL ELSE {fn}({x}, {y}) END)"
    raise AssertionError(f"no SQL for scalar {op}")  # pragma: no cover


def _window(n: int) -> str:
    return f"({_ORDER} ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW)"


def _full(check: str, n: int, value: str) -> str:
    """``value`` when the window holds ``n`` non-empty ``check`` values."""
    return f"(CASE WHEN COUNT({check}) OVER {_window(n)} = {n} THEN {value} END)"


def _rolling(node: Call, a: list[str]) -> str:
    op, x = node.op, a[0]
    n = int(node.params[0])
    w = _window(n)
    if op == "Ref":
        if n == 0:
            return x
        fn = "LAG" if n > 0 else "LEAD"
        return f"{fn}({x}, {abs(n)}) OVER ({_ORDER})"
    if op == "Delta":
        return f"({x} - LAG({x}, {n}) OVER ({_ORDER}))"
    aggregates = {
        "Mean": "AVG",
        "Sum": "SUM",
        "Std": "STDDEV_SAMP",
        "Min": "MIN",
        "Max": "MAX",
    }
    if op in aggregates:
        return _full(x, n, f"{aggregates[op]}({x}) OVER {w}")
    if op == "Quantile":
        return _full(x, n, f"QUANTILE_CONT({x}, {node.params[1]!r}) OVER {w}")
    t = "CAST(_rn AS DOUBLE)"
    if op == "Slope":
        return _full(x, n, f"REGR_SLOPE({x}, {t}) OVER {w}")
    if op == "Rsquare":
        # REGR_R2 is 1.0 for a flat window; the R-squared is undefined there
        r2 = f"(CASE WHEN VAR_POP({x}) OVER {w} > 0 THEN REGR_R2({x}, {t}) OVER {w} END)"
        return _full(x, n, r2)
    if op == "Resi":
        fit = f"(REGR_INTERCEPT({x}, {t}) OVER {w} + REGR_SLOPE({x}, {t}) OVER {w} * {t})"
        return _full(x, n, f"({x} - {fit})")
    values = f"LIST({x}) OVER {w}"
    if op == "Rank":
        # scipy percentileofscore(kind="rank"): today's value is in the window
        below = f"LEN(LIST_FILTER({values}, lambda v: v < {x}))"
        upto = f"LEN(LIST_FILTER({values}, lambda v: v <= {x}))"
        return _full(x, n, f"(({below} + {upto} + 1) / {2.0 * n!r})")
    if op in ("IdxMax", "IdxMin"):
        pick = "LIST_MAX" if op == "IdxMax" else "LIST_MIN"
        return _full(x, n, f"CAST(LIST_POSITION({values}, {pick}({values})) AS DOUBLE)")
    if op == "Corr":
        y = a[1]
        # CORR is NaN (not NULL) when a side is constant: keep the rule that a
        # non-finite value is empty, or it ranks first and passes comparisons
        corr = f"CORR({x}, {y}) OVER {w}"
        return _full(f"({x} + {y})", n, f"(CASE WHEN isfinite({corr}) THEN {corr} END)")
    raise AssertionError(f"no SQL for rolling {op}")  # pragma: no cover


def _csrank(x: str) -> str:
    out = f"(({x}) IS NULL OR NOT member)"
    ordered = f"(PARTITION BY timestamp, {out} ORDER BY {x})"
    whole = f"(PARTITION BY timestamp, {out})"
    count = f"COUNT(*) OVER {whole}"
    return (
        f"(CASE WHEN {out} THEN NULL ELSE "
        f"(RANK() OVER {ordered} + CUME_DIST() OVER {ordered} * {count}) / (2.0 * {count}) END)"
    )


def compile_sql(node: Node, source: str) -> str:
    """A query over ``source`` returning ``ticker, timestamp, value`` for
    every member row, ``value`` in the units known on that row's date."""
    compiler = _Compiler()
    sql, level = compiler.compile(node)
    root = _rescale(sql, dims(node))
    ctes = [
        f"t0 AS (SELECT *, ROW_NUMBER() OVER ({_ORDER}) AS _rn FROM {source})",
    ]
    for k, columns in enumerate(compiler.layers):
        if not columns:
            ctes.append(f"t{k + 1} AS (SELECT * FROM t{k})")
            continue
        cols = ", ".join(f"{expr} AS {name}" for name, expr in columns)
        ctes.append(f"t{k + 1} AS (SELECT *, {cols} FROM t{k})")
    value = f"CAST({root} AS DOUBLE)"
    return (
        f"WITH {', '.join(ctes)} "
        f"SELECT ticker, timestamp, "
        f"CASE WHEN isfinite({value}) THEN {value} END AS value "
        f"FROM t{level} WHERE member"
    )
