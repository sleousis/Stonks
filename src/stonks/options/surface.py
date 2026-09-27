"""Volatility surface basics (roadmap 17.2).

A :class:`VolSurface` is built from ``(time, log_moneyness, iv)`` points of
one underlying on one day, where ``log_moneyness = ln(strike / forward)``.

- Within one expiry the smile is linear in log-moneyness between quoted
  strikes and flat beyond the outermost ones.
- Across expiries it interpolates total variance ``iv^2 x time`` linearly
  in time, flat in vol before the first and after the last expiry.

An SVI fit can replace the smile later behind the same class.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SurfacePoint:
    time: float
    log_moneyness: float
    iv: float


class VolSurface:
    def __init__(self, points: Iterable[SurfacePoint]) -> None:
        by_time: dict[float, list[tuple[float, float]]] = {}
        for p in points:
            if p.time <= 0 or not math.isfinite(p.iv) or p.iv <= 0:
                continue
            by_time.setdefault(p.time, []).append((p.log_moneyness, p.iv))
        if not by_time:
            raise ValueError("a volatility surface needs at least one valid point")
        self._times = sorted(by_time)
        self._smiles: dict[float, tuple[np.ndarray, np.ndarray]] = {}
        for t in self._times:
            pts = sorted(by_time[t])
            self._smiles[t] = (
                np.array([k for k, _ in pts], dtype=float),
                np.array([v for _, v in pts], dtype=float),
            )

    @property
    def times(self) -> list[float]:
        return list(self._times)

    def smile_vol(self, time: float, log_moneyness: float) -> float:
        """The smile of the quoted expiry ``time`` at ``log_moneyness``."""
        ks, vs = self._smiles[time]
        return float(np.interp(log_moneyness, ks, vs))

    def vol(self, time: float, log_moneyness: float = 0.0) -> float:
        """Implied volatility at any ``time`` (years) and log-moneyness."""
        if time <= self._times[0]:
            return self.smile_vol(self._times[0], log_moneyness)
        if time >= self._times[-1]:
            return self.smile_vol(self._times[-1], log_moneyness)
        i = int(np.searchsorted(self._times, time))
        t0, t1 = self._times[i - 1], self._times[i]
        w0 = self.smile_vol(t0, log_moneyness) ** 2 * t0
        w1 = self.smile_vol(t1, log_moneyness) ** 2 * t1
        w = w0 + (w1 - w0) * (time - t0) / (t1 - t0)
        return math.sqrt(max(w, 0.0) / time)

    def atm_term_structure(self) -> list[tuple[float, float]]:
        """``(time, at-the-money vol)`` for every quoted expiry."""
        return [(t, self.smile_vol(t, 0.0)) for t in self._times]
