"""ArtifactBundle — the on-disk representation of a registered strategy.

Layout::

    data/artifacts/<strategy_id>/
    ├── meta.json            # class_path, created_at, stonks version
    ├── params.json          # the Params dict
    ├── fitted_state.joblib  # optional; rule-based strategies skip
    └── reports/
        ├── oos.json
        ├── drift.json
        └── ...
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stonks.core.protocols import SurvivalReport

try:  # prefer a real package version when installed
    from stonks import __version__ as _STONKS_VERSION
except ImportError:  # pragma: no cover
    _STONKS_VERSION = "0.1.0"


@dataclass
class ArtifactBundle:
    path: Path
    class_path: str
    params: dict[str, Any]
    reports: list[SurvivalReport] = field(default_factory=list)
    created_at: str | None = None

    def save(self) -> None:
        base = Path(self.path)
        base.mkdir(parents=True, exist_ok=True)
        (base / "reports").mkdir(parents=True, exist_ok=True)

        created_at = self.created_at or _iso_now()
        self.created_at = created_at

        meta = {
            "class_path": self.class_path,
            "created_at": created_at,
            "stonks_version": _STONKS_VERSION,
        }
        (base / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
        (base / "params.json").write_text(json.dumps(self.params, indent=2, sort_keys=True))

        for report in self.reports:
            (base / "reports" / f"{report.test_id}.json").write_text(
                json.dumps(
                    {
                        "test_id": report.test_id,
                        "passed": bool(report.passed),
                        "metrics": dict(report.metrics),
                        "notes": report.notes,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )

    @classmethod
    def load(cls, path: Path) -> ArtifactBundle:
        base = Path(path)
        meta = json.loads((base / "meta.json").read_text())
        params = json.loads((base / "params.json").read_text())
        reports_dir = base / "reports"
        reports: list[SurvivalReport] = []
        if reports_dir.is_dir():
            for report_path in sorted(reports_dir.glob("*.json")):
                doc = json.loads(report_path.read_text())
                reports.append(
                    SurvivalReport(
                        test_id=doc["test_id"],
                        passed=bool(doc["passed"]),
                        metrics=doc.get("metrics", {}),
                        notes=doc.get("notes", ""),
                    )
                )
        return cls(
            path=base,
            class_path=meta["class_path"],
            params=params,
            reports=reports,
            created_at=meta.get("created_at"),
        )


def load_reports(path: Path) -> Sequence[SurvivalReport]:
    return ArtifactBundle.load(path).reports


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
