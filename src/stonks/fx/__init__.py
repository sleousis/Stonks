"""FX conversion (roadmap 20.5): the one place amounts change currency.

Rates live in the lake (``fx_rates``, filled by ``stonks ingest fx``). A
rate is the latest on or before the day converted, the inverse pair when
only that one is stored, or a cross through USD. A missing rate is never
guessed: :meth:`FxRates.convert` returns ``None`` and callers show the
unconverted amount and say which currency had no rate.
"""

from stonks.fx.rates import (
    FxRateMissing,
    FxRates,
    load_fx_rates,
    normalize_currency,
    sum_in_base,
)

__all__ = ["FxRateMissing", "FxRates", "load_fx_rates", "normalize_currency", "sum_in_base"]
