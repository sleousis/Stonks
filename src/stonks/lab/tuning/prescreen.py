"""The ``prescreen`` tuner: a vectorised screen of a large grid, then the
full backtest on the best sets (BL-49). It lives in ``lab.vectorized`` next
to the fast backtest it uses; importing it here registers it with the tuner
registry like every other module of this package."""

from __future__ import annotations

from stonks.lab.vectorized import PrescreenTuner

__all__ = ["PrescreenTuner"]
