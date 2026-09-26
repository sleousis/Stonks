"""Portfolio construction: normalised signals -> target weights -> orders (BL-08).

- :mod:`.signals` puts every strategy's scores on one scale.
- :mod:`.base` holds the :class:`PortfolioConstructor` seam and its registry.
- :mod:`.constructors` holds the built-in constructors.
- :mod:`.orders` diffs target weights against the book with a no-trade buffer.
"""

from stonks.portfolio.base import (
    ConstructionInput,
    ConstructorSettings,
    PortfolioConstructor,
    TargetBook,
    constructor_names,
    get_constructor,
    register_constructor,
)
from stonks.portfolio.orders import orders_from_targets
from stonks.portfolio.signals import SignalContext, normalize

__all__ = [
    "ConstructionInput",
    "ConstructorSettings",
    "PortfolioConstructor",
    "SignalContext",
    "TargetBook",
    "constructor_names",
    "get_constructor",
    "normalize",
    "orders_from_targets",
    "register_constructor",
]
