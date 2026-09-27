"""``[factors]`` settings (roadmap 22.2): where factor panels are cached."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.factors.cache import MemoryPanelCache, PanelCache, ParquetPanelCache

__all__ = ["FactorSettings", "make_panel_cache"]

#: Folder next to the lake file when ``cache_dir`` is not set.
DEFAULT_CACHE_FOLDER = "factor_panels"


class FactorSettings(BaseModel):
    """``[factors]``: the panel cache."""

    model_config = ConfigDict(extra="forbid")

    #: ``parquet``: one file per panel, kept across restarts; ``memory``: this
    #: process only; ``off``: compute every time.
    cache: Literal["parquet", "memory", "off"] = "parquet"
    #: Folder for Parquet panels (default: ``factor_panels`` next to the lake).
    cache_dir: Path | None = None
    #: Panels kept by the memory cache.
    memory_panels: int = Field(default=64, ge=1, le=10_000)


def make_panel_cache(settings: FactorSettings, lake_path: Path) -> PanelCache | None:
    """The cache ``settings`` ask for (``None`` when it is off)."""
    if settings.cache == "off":
        return None
    if settings.cache == "memory":
        return MemoryPanelCache(settings.memory_panels)
    root = settings.cache_dir or Path(lake_path).parent / DEFAULT_CACHE_FOLDER
    return ParquetPanelCache(root)
