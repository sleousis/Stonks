"""RuleSpec — the versioned, validated JSON document behind ``RuleStrategy``.

A spec is pure data: indicator definitions drawn from a closed registry,
entry / exit conditions as boolean expression trees over those
indicators, a ranking score, sizing and optional percentage stops. The
interpreter in :mod:`stonks.strategies.rules.interpreter` walks the
validated tree; nothing in a spec is ever evaluated as code.

Validation errors come back as a list of :class:`SpecIssue` with a dotted
path (``entry.conditions[1].left.id``) so a UI can point at the field.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError

from stonks.core.interval import Interval
from stonks.core.types import AssetClass

SPEC_VERSION = 1
MAX_PERIOD = 1000
MAX_DEPTH = 16
MAX_NODES = 256

IndicatorId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,31}$"),
]
PriceField = Literal["open", "high", "low", "close", "volume"]
ComparisonOp = Literal["<", "<=", ">", ">=", "crosses_above", "crosses_below"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- indicators -------------------------------------------------------------


class _Indicator(_Strict):
    id: IndicatorId


class CloseIndicator(_Indicator):
    """The bar's close."""

    kind: Literal["close"]


class VolumeIndicator(_Indicator):
    """The bar's volume."""

    kind: Literal["volume"]


class SmaIndicator(_Indicator):
    """Simple moving average of ``source`` over ``period`` bars."""

    kind: Literal["sma"]
    period: int = Field(ge=1, le=MAX_PERIOD)
    source: PriceField = "close"


class EmaIndicator(_Indicator):
    """Exponential moving average (span ``period``) of ``source``."""

    kind: Literal["ema"]
    period: int = Field(ge=1, le=MAX_PERIOD)
    source: PriceField = "close"


class RsiIndicator(_Indicator):
    """Wilder RSI of closes (0..100)."""

    kind: Literal["rsi"]
    period: int = Field(ge=2, le=MAX_PERIOD)


class RocIndicator(_Indicator):
    """Fractional change of closes over ``period`` bars (0.05 = +5%)."""

    kind: Literal["roc", "trailing_return"]
    period: int = Field(ge=1, le=MAX_PERIOD)


class ZScoreIndicator(_Indicator):
    """Rolling z-score of ``source`` over ``period`` bars."""

    kind: Literal["zscore"]
    period: int = Field(ge=2, le=MAX_PERIOD)
    source: PriceField = "close"


class AtrIndicator(_Indicator):
    """Average true range: mean of the true range over ``period`` bars."""

    kind: Literal["atr"]
    period: int = Field(ge=1, le=MAX_PERIOD)


class DonchianHighIndicator(_Indicator):
    """Highest high of the ``period`` bars *before* the current one."""

    kind: Literal["donchian_high"]
    period: int = Field(ge=1, le=MAX_PERIOD)


class DonchianLowIndicator(_Indicator):
    """Lowest low of the ``period`` bars *before* the current one."""

    kind: Literal["donchian_low"]
    period: int = Field(ge=1, le=MAX_PERIOD)


class EfficiencyRatioIndicator(_Indicator):
    """Kaufman efficiency ratio of closes over ``period`` bars (0..1)."""

    kind: Literal["efficiency_ratio"]
    period: int = Field(default=10, ge=1, le=MAX_PERIOD)


class KamaIndicator(_Indicator):
    """Kaufman adaptive moving average of closes: efficiency ratio over
    ``period`` bars, smoothing between ``fast`` and ``slow`` EMA spans."""

    kind: Literal["kama"]
    period: int = Field(default=10, ge=1, le=MAX_PERIOD)
    fast: int = Field(default=2, ge=1, le=MAX_PERIOD)
    slow: int = Field(default=30, ge=2, le=MAX_PERIOD)

    @model_validator(mode="after")
    def _fast_below_slow(self) -> KamaIndicator:
        if self.fast >= self.slow:
            raise ValueError(f"kama fast ({self.fast}) must be below slow ({self.slow})")
        return self


Indicator = Annotated[
    CloseIndicator
    | VolumeIndicator
    | SmaIndicator
    | EmaIndicator
    | RsiIndicator
    | RocIndicator
    | ZScoreIndicator
    | AtrIndicator
    | DonchianHighIndicator
    | DonchianLowIndicator
    | EfficiencyRatioIndicator
    | KamaIndicator,
    Field(discriminator="kind"),
]

INDICATOR_KINDS: tuple[str, ...] = (
    "close",
    "volume",
    "sma",
    "ema",
    "rsi",
    "roc",
    "trailing_return",
    "zscore",
    "atr",
    "donchian_high",
    "donchian_low",
    "efficiency_ratio",
    "kama",
)


# ---- expression trees -------------------------------------------------------


class IndicatorOperand(_Strict):
    type: Literal["indicator"]
    id: IndicatorId


class ConstantOperand(_Strict):
    type: Literal["constant"]
    value: float = Field(allow_inf_nan=False)


Operand = Annotated[IndicatorOperand | ConstantOperand, Field(discriminator="type")]


class CompareCondition(_Strict):
    """``left op right``. ``crosses_above`` is true on the one bar where
    ``left`` moves from ``<= right`` (previous bar) to ``> right``
    (current bar); ``crosses_below`` mirrors it."""

    type: Literal["compare"]
    left: Operand
    op: ComparisonOp
    right: Operand


class AllCondition(_Strict):
    type: Literal["all"]
    conditions: list[Condition] = Field(min_length=1, max_length=32)


class AnyCondition(_Strict):
    type: Literal["any"]
    conditions: list[Condition] = Field(min_length=1, max_length=32)


class NotCondition(_Strict):
    type: Literal["not"]
    condition: Condition


Condition = Annotated[
    CompareCondition | AllCondition | AnyCondition | NotCondition,
    Field(discriminator="type"),
]

for _model in (AllCondition, AnyCondition, NotCondition):
    _model.model_rebuild()


# ---- the rest of the spec ---------------------------------------------------


class UniverseFilter(_Strict):
    asset_classes: list[AssetClass] = Field(default_factory=lambda: ["equity"], min_length=1)
    #: Optional allow-list; ``None`` means every ticker of those classes.
    tickers: list[str] | None = None


class RankSpec(_Strict):
    """Which indicator ranks entry candidates, and in which direction."""

    by: IndicatorId
    order: Literal["desc", "asc"] = "desc"


class Sizing(_Strict):
    """Equal weight: each new position gets ``allocation * equity /
    max_positions`` (capped by cash). At most ``top_k`` new entries per
    decision (default: as many as free slots)."""

    max_positions: int = Field(default=5, ge=1, le=100)
    top_k: int | None = Field(default=None, ge=1, le=100)
    allocation: float = Field(default=1.0, gt=0.0, le=1.0)


class RiskExits(_Strict):
    """Exits evaluated on closes.

    ``stop_loss_pct`` and ``take_profit_pct`` compare with the entry price.
    The trailing stops (Carver, *Leveraged Trading*) sit below the highest
    close since entry and never move down:

    - ``trailing_stop_vol_multiple`` x annualised volatility of close
      returns x price (Carver uses 0.5);
    - ``trailing_stop_atr_multiple`` x the average true range.

    Both read the last ``trailing_stop_period`` bars. When both are set the
    tighter one wins."""

    stop_loss_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    take_profit_pct: float | None = Field(default=None, gt=0.0, le=100.0)
    trailing_stop_vol_multiple: float | None = Field(default=None, ge=0.25, le=3.0)
    trailing_stop_atr_multiple: float | None = Field(default=None, ge=1.0, le=6.0)
    trailing_stop_period: int = Field(default=20, ge=2, le=MAX_PERIOD)

    @property
    def has_trailing_stop(self) -> bool:
        return (
            self.trailing_stop_vol_multiple is not None
            or self.trailing_stop_atr_multiple is not None
        )

    @property
    def has_price_exit(self) -> bool:
        """True when any exit compares with the entry price or its high."""
        return (
            self.stop_loss_pct is not None
            or self.take_profit_pct is not None
            or self.has_trailing_stop
        )


class RuleSpec(_Strict):
    version: Literal[1]
    name: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    interval: str = "1d"
    universe: UniverseFilter = Field(default_factory=UniverseFilter)
    indicators: list[Indicator] = Field(min_length=1, max_length=32)
    entry: Condition
    exit: Condition | None = None
    #: Sell a held ticker as soon as its entry condition is no longer true.
    exit_when_entry_false: bool = False
    rank: RankSpec
    sizing: Sizing = Field(default_factory=Sizing)
    risk: RiskExits = Field(default_factory=RiskExits)

    @field_validator("interval")
    @classmethod
    def _interval(cls, value: str) -> str:
        try:
            return Interval.parse(value).code
        except (ValueError, TypeError) as exc:
            raise ValueError(f"unknown interval {value!r}: {exc}") from None

    @model_validator(mode="after")
    def _semantics(self) -> RuleSpec:
        issues = list(_semantic_issues(self))
        if issues:
            raise _SpecIssues(issues)
        return self

    def indicator_ids(self) -> list[str]:
        return [ind.id for ind in self.indicators]


# ---- validation -------------------------------------------------------------


@dataclass(frozen=True)
class SpecIssue:
    path: str
    message: str


class RuleSpecError(ValueError):
    """A spec failed validation; ``issues`` pinpoints every problem."""

    def __init__(self, issues: list[SpecIssue]) -> None:
        self.issues = issues
        joined = "; ".join(f"{i.path or '<root>'}: {i.message}" for i in issues)
        super().__init__(f"invalid rule spec: {joined}")


class _SpecIssues(ValueError):
    def __init__(self, issues: list[SpecIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues))


_TAGS = frozenset(INDICATOR_KINDS) | {"compare", "all", "any", "not", "indicator", "constant"}


def validate_spec(data: Any) -> RuleSpec:
    """Parse ``data`` (a mapping or a JSON string) into a :class:`RuleSpec`,
    raising :class:`RuleSpecError` with every issue found."""
    if isinstance(data, RuleSpec):
        return data
    if isinstance(data, str | bytes):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise RuleSpecError([SpecIssue("", f"not valid JSON: {exc}")]) from None
    if not isinstance(data, dict):
        raise RuleSpecError([SpecIssue("", "a rule spec must be a JSON object")])
    try:
        return RuleSpec.model_validate(data)
    except PydanticValidationError as exc:
        raise RuleSpecError(_issues_from(exc)) from None


def _issues_from(exc: PydanticValidationError) -> list[SpecIssue]:
    issues: list[SpecIssue] = []
    for err in exc.errors():
        original = (err.get("ctx") or {}).get("error")
        if isinstance(original, _SpecIssues):
            issues.extend(original.issues)
            continue
        loc = [p for p in err["loc"] if not (isinstance(p, str) and p in _TAGS)]
        if err["type"] in ("union_tag_invalid", "union_tag_not_found"):
            loc.append(str((err.get("ctx") or {}).get("discriminator", "")).strip("'"))
        issues.append(SpecIssue(_format_path(loc), err["msg"]))
    return issues


def _format_path(loc: list[Any]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _semantic_issues(spec: RuleSpec) -> Iterator[SpecIssue]:
    known: set[str] = set()
    for i, ind in enumerate(spec.indicators):
        if ind.id in known:
            yield SpecIssue(f"indicators[{i}].id", f"duplicate indicator id {ind.id!r}")
        known.add(ind.id)
    if spec.rank.by not in known:
        yield SpecIssue("rank.by", f"unknown indicator {spec.rank.by!r}")
    for name in ("entry", "exit"):
        cond = getattr(spec, name)
        if cond is None:
            continue
        depth, nodes = _shape(cond)
        if depth > MAX_DEPTH:
            yield SpecIssue(name, f"condition nested too deep ({depth} > {MAX_DEPTH} levels)")
            continue
        if nodes > MAX_NODES:
            yield SpecIssue(name, f"condition has too many nodes ({nodes} > {MAX_NODES})")
            continue
        yield from _condition_issues(cond, name, known)


def _shape(cond: Any) -> tuple[int, int]:
    """(depth, node count) of a condition tree, iteratively."""
    max_depth, nodes = 0, 0
    stack = [(cond, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        if isinstance(node, AllCondition | AnyCondition):
            stack.extend((c, depth + 1) for c in node.conditions)
        elif isinstance(node, NotCondition):
            stack.append((node.condition, depth + 1))
    return max_depth, nodes


def _condition_issues(cond: Any, path: str, known: set[str]) -> Iterator[SpecIssue]:
    if isinstance(cond, AllCondition | AnyCondition):
        for i, child in enumerate(cond.conditions):
            yield from _condition_issues(child, f"{path}.conditions[{i}]", known)
    elif isinstance(cond, NotCondition):
        yield from _condition_issues(cond.condition, f"{path}.condition", known)
    else:
        sides = (("left", cond.left), ("right", cond.right))
        if all(isinstance(op, ConstantOperand) for _, op in sides):
            yield SpecIssue(path, "at least one side of a comparison must be an indicator")
        for side, operand in sides:
            if isinstance(operand, IndicatorOperand) and operand.id not in known:
                yield SpecIssue(f"{path}.{side}.id", f"unknown indicator {operand.id!r}")


def rule_spec_json_schema() -> dict[str, Any]:
    """JSON Schema of :class:`RuleSpec` for the UI rule builder."""
    return RuleSpec.model_json_schema()


def referenced_indicators(cond: Any) -> set[str]:
    """Indicator ids a condition tree reads."""
    found: set[str] = set()
    stack = [cond]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, AllCondition | AnyCondition):
            stack.extend(node.conditions)
        elif isinstance(node, NotCondition):
            stack.append(node.condition)
        else:
            for operand in (node.left, node.right):
                if isinstance(operand, IndicatorOperand):
                    found.add(operand.id)
    return found
