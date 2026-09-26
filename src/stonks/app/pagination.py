"""Offset pagination shared by every list operation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class PageWindow(Protocol):
    """What :func:`page_of` needs from a request: ``api.deps.PageParams``."""

    @property
    def limit(self) -> int: ...

    @property
    def offset(self) -> int: ...


def page_of[T](items: Sequence[T], window: PageWindow) -> Page[T]:
    """One page of a list already read in full: for small per-user lists
    (tokens, halts, portfolios, ...) where a SQL ``LIMIT`` buys nothing.
    ``total`` counts the whole list."""
    start = window.offset
    return Page[T](
        items=list(items[start : start + window.limit]),
        total=len(items),
        limit=window.limit,
        offset=window.offset,
    )
