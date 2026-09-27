"""The optional lab-worker Compose service (roadmap 14.9)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "deploy" / "compose.yaml"


def _default(value: object) -> str:
    """``${VAR:-default}`` -> ``default``."""
    text = str(value)
    return text.split(":-", 1)[1].rstrip("}") if ":-" in text else text


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def test_the_lab_worker_is_off_by_default_behind_a_profile():
    worker = _services()["lab-worker"]
    assert worker["profiles"] == ["lab-worker"]
    assert worker["command"] == ["python", "-m", "stonks.lab.offload", "worker"]


def test_the_lab_worker_has_its_own_cpu_and_memory_limits():
    worker = _services()["lab-worker"]
    cpus = _default(worker["cpus"])
    assert float(cpus) >= 1
    assert _default(worker["mem_limit"]).endswith("g")
    # The process pool uses as many workers as the container has CPUs.
    assert _default(worker["environment"]["STONKS_LAB_MAX_WORKERS"]) == cpus
    # The API wins when both want the CPU.
    assert int(worker["cpu_shares"]) < 1024


def test_the_lab_worker_shares_the_data_volume_and_runs_as_a_worker():
    services = _services()
    worker = services["lab-worker"]
    assert "data:/data" in worker["volumes"]
    env = worker["environment"]
    assert env["STONKS_DATA_DIR"] == "/data"
    assert env["STONKS_LAB_EXECUTOR"] == "worker"
    # Pool snapshots go on the data volume, not the in-memory /tmp.
    assert env["TMPDIR"].startswith("/data/")
    # The API queues lab jobs only when the operator turns the worker on.
    assert _default(services["api"]["environment"]["STONKS_LAB_EXECUTOR"]) == "in_process"
