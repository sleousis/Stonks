"""Cached factor panels (roadmap 22.2).

:class:`FactorEngine` is the one way research code gets a panel: it keys
the panel by factor, universe, window, interval and sampled dates plus a
fingerprint of the data read (:mod:`stonks.factors.cache`), serves a hit
from the cache and computes a miss. A factor over a point-in-time view is
never cached (its fingerprint is unknown); the view's own session memo
covers that case.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd

from stonks.core.timeutil import day_end, day_start
from stonks.factors.base import Factor
from stonks.factors.cache import PanelCache, PanelKey, data_fingerprint, slot_of
from stonks.factors.engine import PanelRequest, warmup_days
from stonks.logging import get_logger

__all__ = ["FactorEngine"]

_log = get_logger("stonks.factors.panels")


class FactorEngine:
    """Panels of any :class:`Factor` over one lake, through an optional cache."""

    def __init__(self, lake: Any, cache: PanelCache | None = None) -> None:
        self.lake = lake
        self.cache = cache

    def panel(
        self,
        factor: Factor,
        request: PanelRequest,
        dates: pd.DatetimeIndex | None = None,
    ) -> pd.DataFrame:
        """Dates by tickers for ``request`` (at ``dates`` only, when given).
        A whole-window factor is computed and cached for every bar and
        sliced to ``dates``; a per-date factor is computed at ``dates``."""
        sample = None if factor.whole_window or dates is None else dates
        key = self._key(factor, request, sample)
        panel = self.cache.get(key) if (self.cache is not None and key is not None) else None
        if panel is None:
            panel = factor.panel(self.lake, request, sample)
            if self.cache is not None and key is not None:
                self.cache.put(key, panel)
        else:
            _log.debug("factor.cache.hit", factor=factor.id)
        if dates is not None:
            panel = panel.reindex(pd.DatetimeIndex(dates))
        return panel

    def _key(
        self, factor: Factor, request: PanelRequest, sample: pd.DatetimeIndex | None
    ) -> PanelKey | None:
        if self.cache is None:
            return None
        warm = warmup_days(factor.lookback_bars, request.interval)
        fingerprint = data_fingerprint(
            self.lake,
            request.universe,
            request.interval.code,
            day_start(request.start - timedelta(days=warm)),
            day_end(request.end),
            tables=factor.tables,
            membership=request.membership,
        )
        if fingerprint is None:
            return None
        extra = {
            "universe_id": request.universe_id,
            "dates": None if sample is None else [str(d) for d in sample],
        }
        slot = slot_of(
            factor.cache_token,
            request.universe,
            (request.start, request.end),
            request.interval.code,
            extra,
        )
        return PanelKey(slot, fingerprint)
