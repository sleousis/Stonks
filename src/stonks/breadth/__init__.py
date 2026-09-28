"""Market breadth (roadmap 23.14): how many stocks take part in a move.

Display only. Nothing here trades or feeds a rule. The service reads daily
bars from the lake and :func:`market_breadth` turns them into counts and
plain words.
"""

from stonks.breadth.compute import (
    AboveAverage,
    Breadth,
    BreadthLine,
    market_breadth,
)
from stonks.breadth.settings import BreadthSettings

__all__ = ["AboveAverage", "Breadth", "BreadthLine", "BreadthSettings", "market_breadth"]
