"""The numeric grounding check (roadmap 23.8).

Small models invent numbers. Every number in a reply must appear in a
tool result of that turn (or in the person's own message). Matching is
tolerant:

- rounding: a number written with ``d`` decimals matches any value within
  half a unit of its last digit (``1.4`` matches ``1.4237``);
- percent: ``1.6%`` matches ``0.0164`` (a fraction) and ``1.6`` (a percent
  field);
- units: ``1.2M``, ``10.5k``, ``3bn``, ``2 million`` are scaled first;
- sign: ``a loss of 2.1%`` matches ``-0.021``;
- text: numbers inside strings of a tool result count, and so does the
  length of every list (``5 positions``).

Dates, times, list markers and numbers glued to letters (``P21``, ``Q3``)
are not checked. Small integers (``free_integers_upto``) and years are
free by default. The loop (:mod:`stonks.assistant.loop`) flags a reply that
fails, or asks the model once to rewrite it citing its tools.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GroundingMode = Literal["off", "flag", "rewrite"]


class GroundingSettings(BaseModel):
    """``[assistant.grounding]``: the numeric grounding check."""

    model_config = ConfigDict(extra="forbid")

    #: off: no check. flag: add a note naming the numbers no tool returned.
    #: rewrite: ask the model once to cite or drop them, then flag if needed.
    mode: GroundingMode = "flag"
    #: Whole numbers up to this are not checked ("2 ideas").
    free_integers_upto: int = Field(default=3, ge=0, le=1000)
    #: Four-digit years 1900 to 2100 are not checked.
    ignore_years: bool = True


_SCALES: dict[str, float] = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_MARKER = re.compile(r"(?m)^\s*\d+[.)](?=\s)")
_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-+−])?(?P<cur>[$€£])?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
    r"(?:\s?(?P<unit>%|thousand|million|billion|mn|bn|[kKmMbB])(?![\w]))?"
    r"(?![\w])"
)


@dataclass(frozen=True)
class NumberMention:
    """A number as written in the text."""

    text: str
    #: The written value, without sign or unit.
    value: float
    decimals: int
    scale: float = 1.0
    percent: bool = False
    negative: bool = False
    currency: bool = False

    @property
    def is_integer(self) -> bool:
        return self.decimals == 0 and self.scale == 1.0 and not self.percent

    def targets(self) -> list[tuple[float, float]]:
        """``(value, tolerance)`` pairs this mention may stand for."""
        half = 0.5 * 10.0**-self.decimals
        out = [(self.value * self.scale, half * self.scale)]
        if self.percent:
            out.append((self.value / 100.0, half / 100.0))
        return out


@dataclass(frozen=True)
class GroundingReport:
    ok: bool
    #: Numbers checked (free ones left out).
    checked: int
    #: The mentions no tool result supports, as written.
    ungrounded: list[str] = field(default_factory=list[str])

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checked": self.checked, "ungrounded": list(self.ungrounded)}


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def extract_numbers(text: str) -> list[NumberMention]:
    """The numbers of ``text`` a reader would take as figures."""
    cleaned = _MARKER.sub(_blank, _TIME.sub(_blank, _DATE.sub(_blank, text or "")))
    out: list[NumberMention] = []
    for m in _NUMBER.finditer(cleaned):
        whole = m.group("int").replace(",", "")
        dec = m.group("dec") or ""
        unit = (m.group("unit") or "").lower()
        out.append(
            NumberMention(
                text=m.group(0).strip(),
                value=float(f"{whole}.{dec}" if dec else whole),
                decimals=len(dec),
                scale=_SCALES.get(unit, 1.0),
                percent=unit == "%",
                negative=m.group("sign") in ("-", "−"),
                currency=m.group("cur") is not None,
            )
        )
    return out


def grounded_values(payloads: Iterable[Any]) -> set[float]:
    """Every number a set of tool results holds (absolute values)."""
    found: set[float] = set()
    stack: list[Any] = list(payloads)
    while stack:
        item = stack.pop()
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, int | float):
            found.add(abs(float(item)))
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            found.add(float(len(item)))
            stack.extend(item)
        elif isinstance(item, str):
            parsed = _json(item)
            if parsed is not None:
                stack.append(parsed)
                continue
            for mention in extract_numbers(item):
                for value, _ in mention.targets():
                    found.add(abs(value))
    return found


def check_grounding(
    reply: str,
    payloads: Sequence[Any],
    *,
    user_texts: Sequence[str] = (),
    settings: GroundingSettings | None = None,
) -> GroundingReport:
    """Whether every figure in ``reply`` appears in ``payloads`` (the tool
    results of the turn) or the person's own words."""
    cfg = settings or GroundingSettings()
    values = sorted(grounded_values([*payloads, *user_texts]))
    ungrounded: list[str] = []
    checked = 0
    for mention in extract_numbers(reply):
        if _free(mention, cfg):
            continue
        checked += 1
        if not any(_near(values, target, tol) for target, tol in mention.targets()):
            ungrounded.append(mention.text)
    return GroundingReport(ok=not ungrounded, checked=checked, ungrounded=ungrounded)


def flag_note(report: GroundingReport) -> str:
    """The line added under a reply whose numbers were not all found."""
    shown = ", ".join(report.ungrounded[:8])
    return f"\n\nCheck these numbers: no tool result in this answer shows {shown}."


def rewrite_request(report: GroundingReport) -> str:
    """What the loop asks the model when it rewrites a reply."""
    shown = ", ".join(report.ungrounded[:12])
    return (
        f"Your reply states numbers that no tool result in this turn contains: {shown}. "
        "Rewrite the reply. Keep only numbers you can copy from a tool result, name the tool "
        "each one came from, and drop or call a tool for the rest. Do not call a tool now."
    )


# ---- helpers ---------------------------------------------------------------------


def _free(mention: NumberMention, cfg: GroundingSettings) -> bool:
    if not mention.is_integer or mention.currency:
        return False
    if mention.value <= cfg.free_integers_upto:
        return True
    return cfg.ignore_years and 1900 <= mention.value <= 2100 and len(mention.text) == 4


def _near(values: Sequence[float], target: float, tol: float) -> bool:
    slack = tol + 1e-9 * max(1.0, abs(target))
    lo, hi = abs(target) - slack, abs(target) + slack
    # values is sorted: binary search for the first value >= lo
    left, right = 0, len(values)
    while left < right:
        mid = (left + right) // 2
        if values[mid] < lo:
            left = mid + 1
        else:
            right = mid
    return left < len(values) and values[left] <= hi


def _json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None
