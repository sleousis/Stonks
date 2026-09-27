"""The streaming source registry (roadmap 21.1).

A source is one module in :mod:`stonks.streaming.sources` whose class is
decorated with :func:`register_stream_source`. Adding one never edits a
list. :func:`build_stream_source` builds a registered source from settings.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Callable

from stonks.streaming.base import StreamConfigError, StreamContext, StreamingSource

_SOURCES: dict[str, type[StreamingSource]] = {}
_LOCK = threading.Lock()
_DISCOVERED = False


def register_stream_source(
    source_id: str,
) -> Callable[[type[StreamingSource]], type[StreamingSource]]:
    def decorate(cls: type[StreamingSource]) -> type[StreamingSource]:
        if not source_id or source_id != source_id.lower() or not source_id.isidentifier():
            raise ValueError(f"bad streaming source id {source_id!r}")
        existing = _SOURCES.get(source_id)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"streaming source {source_id!r} is registered twice")
        cls.source_id = source_id
        _SOURCES[source_id] = cls
        return cls

    return decorate


def _discover() -> None:
    global _DISCOVERED
    with _LOCK:
        if _DISCOVERED:
            return
        import stonks.streaming.sources as package

        for info in pkgutil.iter_modules(package.__path__):
            if not info.name.startswith("_"):
                importlib.import_module(f"{package.__name__}.{info.name}")
        _DISCOVERED = True


def stream_source_classes() -> dict[str, type[StreamingSource]]:
    """Every registered streaming source, by id."""
    _discover()
    return dict(sorted(_SOURCES.items()))


def stream_source_class(source_id: str) -> type[StreamingSource]:
    cls = stream_source_classes().get(source_id)
    if cls is None:
        known = ", ".join(stream_source_classes())
        raise StreamConfigError(f"unknown streaming source {source_id!r} (known: {known})")
    return cls


def build_stream_source(ctx: StreamContext, source_id: str | None = None) -> StreamingSource:
    """The source ``source_id`` (default ``[streaming] source``), built from ``ctx``."""
    return stream_source_class(source_id or ctx.settings.streaming.source).from_settings(ctx)
