"""Fit the cost model to minute trading (roadmap 21.3.5, principles P22).

``stonks tca calibrate --interval 1m`` collects two kinds of evidence and
hands them to :func:`fit_minute_costs`:

- **quoted half spreads**, in bps of the mid, from the recorded quotes
  (``streaming.recorder``), per asset class;
- **fill samples**: for each intraday fill, what it paid over the arrival
  price (the next minute's open) in bps, its size and the volume of the
  minute it filled in, and the quoted half spread at the fill when a quote
  was recorded.

The fit, per the square-root model of :mod:`stonks.backtest.costs`::

    cost_bps = half_spread_bps + impact_bps * sqrt(quantity / bar_volume)

- ``half_spread_bps`` per asset class is the median quoted half spread.
- ``impact_bps`` is least squares through the origin of
  ``cost_bps - half_spread`` on ``sqrt(quantity / bar_volume)``, clipped
  to ``[0, max_impact_bps]``. The half spread of a fill is its own quote,
  or its class's fitted median without one.

Too little data keeps the current value and says so in ``notes``. The
result is a **proposal**: :meth:`CostCalibration.to_toml` renders a
``[backtest.costs]`` block for a person to review. Nothing here writes
settings. Point in time is the caller's job: pass only evidence up to
``end``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.core.types import AssetClass

#: The fewest quotes (per class) and fills behind a fitted value.
MIN_QUOTES = 10
MIN_FILLS = 10


@dataclass(frozen=True)
class FillSample:
    """One intraday fill, as calibration evidence."""

    asset_class: AssetClass
    quantity: float
    #: Units traded in the minute the fill landed in; ``None`` when unknown.
    bar_volume: float | None
    #: ``s (F - A) / A`` in bps: paid over the arrival price, fees excluded.
    cost_bps: float
    #: Quoted half spread at the fill in bps; ``None`` without a quote.
    half_spread_bps: float | None
    notional: float
    fee: float


@dataclass(frozen=True)
class CostCalibration:
    """What :func:`fit_minute_costs` found, and the settings it proposes."""

    interval: str
    start: date | None
    end: date
    quotes: dict[str, int]
    fills: int
    fills_used: int
    #: The fitted half spread per asset class (only classes with enough quotes).
    half_spreads: dict[str, float]
    impact_bps: float | None
    impact_r2: float | None
    residual_std_bps: float | None
    #: Realised fees per asset class, in bps of the filled value (for reading).
    fee_bps: dict[str, float]
    current: CostModelSettings
    proposed: CostModelSettings
    notes: list[str] = field(default_factory=list)

    def to_toml(self) -> str:
        span = f"{self.start.isoformat()} to " if self.start else "up to "
        header = [
            f"Proposed by `stonks tca calibrate --interval {self.interval}`,"
            f" data {span}{self.end.isoformat()}.",
            f"Evidence: {sum(self.quotes.values())} quotes, {self.fills_used} of"
            f" {self.fills} fills.",
            "Never applied by Stonks. Review it, then copy it into your config by hand.",
            "With recorded quotes the minute fill model already adds the quoted half",
            "spread, so set half_spread_bps to 0 for books filled that way.",
            *self.notes,
        ]
        return costs_toml(self.proposed, header=header)

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat(),
            "quotes": dict(self.quotes),
            "fills": self.fills,
            "fills_used": self.fills_used,
            "half_spreads": dict(self.half_spreads),
            "impact_bps": self.impact_bps,
            "impact_r2": self.impact_r2,
            "residual_std_bps": self.residual_std_bps,
            "fee_bps": dict(self.fee_bps),
            "notes": list(self.notes),
        }


def fit_minute_costs(
    quoted_half_spreads: Mapping[AssetClass, Sequence[float]],
    samples: Sequence[FillSample],
    current: CostModelSettings,
    *,
    end: date,
    start: date | None = None,
    interval: str = "1m",
    min_quotes: int = MIN_QUOTES,
    min_fills: int = MIN_FILLS,
) -> CostCalibration:
    """Fit half spreads and square-root impact (see the module doc)."""
    notes: list[str] = []
    half_spreads: dict[str, float] = {}
    counts: dict[str, int] = {}
    for cls in sorted(quoted_half_spreads):
        values = [v for v in quoted_half_spreads[cls] if math.isfinite(v) and v >= 0]
        counts[cls] = len(values)
        if len(values) >= min_quotes:
            half_spreads[cls] = float(statistics.median(values))
        else:
            notes.append(f"{cls}: half spread kept, {len(values)} quotes (needs {min_quotes}).")

    def half_spread_of(sample: FillSample) -> float:
        if sample.half_spread_bps is not None and math.isfinite(sample.half_spread_bps):
            return sample.half_spread_bps
        cls = sample.asset_class
        return half_spreads.get(cls, current.for_asset_class(cls).half_spread_bps)

    xs: list[float] = []
    ys: list[float] = []
    for s in samples:
        volume = s.bar_volume
        if volume is None or not math.isfinite(volume) or volume <= 0 or s.quantity <= 0:
            continue
        if not math.isfinite(s.cost_bps):
            continue
        xs.append(math.sqrt(s.quantity / volume))
        ys.append(s.cost_bps - half_spread_of(s))

    impact = r2 = resid_std = None
    if len(xs) >= min_fills and sum(x * x for x in xs) > 0:
        raw = sum(x * y for x, y in zip(xs, ys, strict=True)) / sum(x * x for x in xs)
        impact = min(max(raw, 0.0), current.max_impact_bps)
        residuals = [y - impact * x for x, y in zip(xs, ys, strict=True)]
        mean_y = sum(ys) / len(ys)
        ss_tot = sum((y - mean_y) ** 2 for y in ys)
        ss_res = sum(r * r for r in residuals)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else (1.0 if ss_res < 1e-12 else 0.0)
        resid_std = statistics.pstdev(residuals)
    else:
        notes.append(f"impact kept, {len(xs)} fills with a minute volume (needs {min_fills}).")

    fee_bps: dict[str, float] = {}
    by_class: dict[str, tuple[float, float]] = {}
    for s in samples:
        fees, notional = by_class.get(s.asset_class, (0.0, 0.0))
        by_class[s.asset_class] = (fees + s.fee, notional + s.notional)
    for cls, (fees, notional) in sorted(by_class.items()):
        if notional > 0:
            fee_bps[cls] = fees / notional * 10_000.0

    proposed = _propose(current, half_spreads, impact)
    return CostCalibration(
        interval=interval,
        start=start,
        end=end,
        quotes=counts,
        fills=len(samples),
        fills_used=len(xs),
        half_spreads=half_spreads,
        impact_bps=impact,
        impact_r2=r2,
        residual_std_bps=resid_std,
        fee_bps=fee_bps,
        current=current,
        proposed=proposed,
        notes=notes,
    )


def _propose(
    current: CostModelSettings, half_spreads: Mapping[str, float], impact: float | None
) -> CostModelSettings:
    classes: dict[AssetClass, AssetClassCosts] = dict(current.asset_classes)
    for cls, value in half_spreads.items():
        base = current.for_asset_class(cls)  # type: ignore[arg-type]
        classes[cls] = base.model_copy(update={"half_spread_bps": value})  # type: ignore[index]
    update: dict[str, Any] = {"asset_classes": classes}
    if impact is not None:
        update.update(impact_bps=impact, impact_model="sqrt")
    return CostModelSettings.model_validate({**current.model_dump(), **_dumped(update)})


def _dumped(update: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(update)
    if "asset_classes" in out:
        out["asset_classes"] = {k: v.model_dump() for k, v in out["asset_classes"].items()}
    return out


# ---- TOML -------------------------------------------------------------------------


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(float(value)) if isinstance(value, float) else str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _table(name: str, values: Mapping[str, Any]) -> list[str]:
    lines = [f"[{name}]"]
    lines.extend(f"{key} = {_value(v)}" for key, v in values.items())
    return lines


def costs_toml(settings: CostModelSettings, *, header: Sequence[str] = ()) -> str:
    """``settings`` as a ``[backtest.costs]`` TOML block, comments first."""
    dumped = settings.model_dump()
    scalars = {k: v for k, v in dumped.items() if not isinstance(v, dict) and v is not None}
    lines = [f"# {line}" for line in header]
    lines += _table("backtest.costs", scalars)
    lines += ["", *_table("backtest.costs.default", dumped["default"])]
    lines += ["", *_table("backtest.costs.istar", dumped["istar"])]
    for cls in sorted(dumped["asset_classes"]):
        lines += ["", *_table(f"backtest.costs.asset_classes.{cls}", dumped["asset_classes"][cls])]
    algo = dumped.get("exec_algo")
    if algo:  # roadmap 23.16: the execution algo the fills assume
        lines += ["", *_table("backtest.costs.exec_algo", {"name": algo["name"]})]
        if algo.get("params"):
            lines += ["", *_table("backtest.costs.exec_algo.params", algo["params"])]
    return "\n".join(lines) + "\n"
