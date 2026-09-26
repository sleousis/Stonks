"""ArtifactBundle — the on-disk representation of a registered strategy.

Layout::

    data/artifacts/<strategy_id>/
    ├── meta.json            # class_path, created_at, stonks version (+ strategy's own
    │                        #   keys, + lab provenance: lab_run_id, n_trials_total,
    │                        #   hypothesis, premortem, manifest)
    ├── params.json          # the Params dict
    ├── fitted_state.*       # optional, written by Strategy.save; rule-based strategies skip
    └── reports/
        ├── oos.json
        ├── drift.json
        └── ...

``StrategyRegistry.register`` calls ``Strategy.save`` into the directory
first, then ``ArtifactBundle.save``; the bundle merges into an existing
``meta.json`` rather than replacing it, and rewrites ``params.json`` with the
same params the strategy holds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stonks.core.protocols import SurvivalReport

try:  # prefer a real package version when installed
    from stonks import __version__ as _STONKS_VERSION
except ImportError:  # pragma: no cover
    _STONKS_VERSION = "0.1.0"


#: ``meta.json`` keys the registry owns; extra metadata never overrides them.
REGISTRY_META_KEYS = ("class_path", "created_at", "stonks_version")


@dataclass
class ArtifactBundle:
    path: Path
    class_path: str
    params: dict[str, Any]
    reports: list[SurvivalReport] = field(default_factory=list)
    created_at: str | None = None
    #: Extra ``meta.json`` keys, e.g. lab provenance (``lab_run_id``,
    #: ``n_trials_total``, ``hypothesis``, ``premortem``, ``manifest``; see
    #: ``LabRunResult.artifact_meta``). On load: every non-registry key.
    meta: dict[str, Any] = field(default_factory=dict)

    def save(self) -> None:
        base = Path(self.path)
        base.mkdir(parents=True, exist_ok=True)
        (base / "reports").mkdir(parents=True, exist_ok=True)

        created_at = self.created_at or _iso_now()
        self.created_at = created_at

        # A strategy may already have written its own meta.json into this
        # directory (``Strategy.save``); keep its keys and let the bundle's
        # registry-owned keys win on overlap.
        meta = {
            **_read_meta(base),
            **self.meta,
            "class_path": self.class_path,
            "created_at": created_at,
            "stonks_version": _STONKS_VERSION,
        }
        (base / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True, default=str))
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
            meta={k: v for k, v in meta.items() if k not in REGISTRY_META_KEYS},
        )


def load_reports(path: Path) -> Sequence[SurvivalReport]:
    return ArtifactBundle.load(path).reports


def update_meta(path: Path, extra: Mapping[str, Any]) -> None:
    """Merge ``extra`` into an existing bundle's ``meta.json`` (e.g. lab
    provenance after ``StrategyRegistry.register``). Registry-owned keys
    are never overwritten."""
    base = Path(path)
    meta = _read_meta(base)
    meta.update({k: v for k, v in extra.items() if k not in REGISTRY_META_KEYS})
    (base / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True, default=str))


def _read_meta(base: Path) -> dict[str, Any]:
    meta_path = base / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        loaded = json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
