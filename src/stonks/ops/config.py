"""Backup settings (``[backup]``, roadmap 12.4). ``Settings`` mounts
:class:`BackupConfig` as ``backup``; until then callers fall back to the
defaults here."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class BackupRetention(BaseModel):
    """Grandfather-father-son retention: keep the newest backup of each of
    the last ``daily`` days, ``weekly`` ISO weeks and ``monthly`` months
    that have backups. The newest backup is always kept."""

    model_config = ConfigDict(extra="forbid")

    daily: int = Field(default=7, ge=0)
    weekly: int = Field(default=4, ge=0)
    monthly: int = Field(default=12, ge=0)


class BackupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Where local backups go. None: a ``backups`` folder next to the lake.
    dir: Path | None = None
    retention: BackupRetention = Field(default_factory=BackupRetention)
