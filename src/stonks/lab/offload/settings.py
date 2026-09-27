"""``[lab.offload]``: where heavy lab jobs run (roadmap 14.9)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LabExecutorName = Literal["in_process", "worker"]

#: Job kinds a worker takes by default: tuning plus survival suites (MCPT
#: is a survival test), sweeps, and Studio lab runs.
DEFAULT_OFFLOAD_KINDS: tuple[str, ...] = ("lab_run", "lab_sweep", "studio_lab_run")


class LabOffloadSettings(BaseModel):
    """How the API hands lab jobs to a separate worker process.

    ``executor = "in_process"`` (the default) runs every job inside the API,
    as before. ``"worker"`` queues :attr:`kinds` in the state DB for
    ``python -m stonks.lab.offload worker``, which reads a read-only lake
    snapshot. Env: ``STONKS_LAB_EXECUTOR``."""

    model_config = ConfigDict(extra="forbid")

    executor: LabExecutorName = "in_process"
    kinds: list[str] = Field(default_factory=lambda: list(DEFAULT_OFFLOAD_KINDS))
    #: Where snapshots live. Default: ``<lake dir>/lab_snapshots``.
    snapshot_dir: Path | None = None
    #: A snapshot older than this is rebuilt before the next offloaded job,
    #: even when no ingest ran since.
    snapshot_max_age_minutes: float = Field(default=60.0, gt=0)
    #: Snapshots kept on disk (the newest ones; held ones are never pruned).
    keep_snapshots: int = Field(default=2, ge=1)
    #: An idle worker polls the queue this often.
    poll_seconds: float = Field(default=2.0, gt=0)
    #: A busy worker writes its heartbeat (and checks for a cancel) this often.
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    #: A running worker job with no heartbeat for this long is failed.
    lease_seconds: float = Field(default=120.0, gt=0)

    def snapshot_root(self, lake_path: str | Path) -> Path:
        if self.snapshot_dir is not None:
            return Path(self.snapshot_dir)
        return Path(lake_path).parent / "lab_snapshots"
