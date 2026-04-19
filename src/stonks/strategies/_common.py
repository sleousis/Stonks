"""Re-exports of the cross-layer timeutil helpers under a strategy-local
namespace. Kept as a thin alias so strategy examples import from a single
obvious module rather than reaching into ``core`` directly.
"""

from stonks.core.timeutil import as_datetime, iso

__all__ = ["as_datetime", "iso"]
