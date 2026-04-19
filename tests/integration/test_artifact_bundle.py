"""Unit/integration tests for ArtifactBundle — the on-disk representation of
a strategy (params + optional fitted state + survival reports + metadata).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from stonks.core.protocols import SurvivalReport
from stonks.registry.artifact import ArtifactBundle


def test_bundle_write_and_load(tmp_path):
    bundle = ArtifactBundle(
        path=tmp_path / "strat_1",
        class_path="stonks.strategies.examples.momentum:Momentum",
        params={"lookback_days": 30, "threshold": 0.05, "allocation": 1.0},
        reports=[
            SurvivalReport(
                test_id="oos",
                passed=True,
                metrics={"sharpe_oos": 1.2, "max_drawdown_oos": -0.1},
            ),
            SurvivalReport(
                test_id="drift",
                passed=True,
                metrics={"max_psi": 0.05, "mean_psi": 0.02, "features_compared": 1.0},
            ),
        ],
    )
    bundle.save()

    base = Path(tmp_path / "strat_1")
    meta = json.loads((base / "meta.json").read_text())
    params = json.loads((base / "params.json").read_text())
    reports_files = sorted((base / "reports").glob("*.json"))

    assert meta["class_path"] == "stonks.strategies.examples.momentum:Momentum"
    assert meta["created_at"]
    assert params["lookback_days"] == 30
    assert {p.name for p in reports_files} == {"oos.json", "drift.json"}


def test_bundle_load_round_trips(tmp_path):
    base = tmp_path / "strat_2"
    original = ArtifactBundle(
        path=base,
        class_path="stonks.strategies.examples.buy_and_hold:BuyAndHold",
        params={"ticker": "AAPL.US", "allocation": 1.0},
        reports=[],
    )
    original.save()

    loaded = ArtifactBundle.load(base)
    assert loaded.class_path == original.class_path
    assert loaded.params == original.params
    assert loaded.reports == []


def test_bundle_load_reads_existing_reports(tmp_path):
    base = tmp_path / "strat_3"
    ArtifactBundle(
        path=base,
        class_path="x.y:Z",
        params={"a": 1},
        reports=[SurvivalReport(test_id="t1", passed=True, metrics={"m": 1.0})],
    ).save()

    loaded = ArtifactBundle.load(base)
    assert len(loaded.reports) == 1
    assert loaded.reports[0].test_id == "t1"


def test_bundle_saved_meta_contains_iso_timestamp(tmp_path):
    base = tmp_path / "strat_4"
    ArtifactBundle(
        path=base,
        class_path="x.y:Z",
        params={},
        reports=[],
    ).save()
    meta = json.loads((base / "meta.json").read_text())
    # should parse as ISO-format UTC datetime
    datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
    _ = datetime.now(UTC)
