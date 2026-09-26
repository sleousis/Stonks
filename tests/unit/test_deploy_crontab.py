"""deploy/crontab: a chained job logs every command, not only the last (P-02)."""

from __future__ import annotations

from pathlib import Path

CRONTAB = Path(__file__).resolve().parents[2] / "deploy" / "crontab"
LOG = ">> $HOME/stonks-cron.log 2>&1"


def _jobs() -> list[str]:
    lines = CRONTAB.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_a_chained_job_sends_the_whole_chain_to_the_log():
    chained = [job for job in _jobs() if "&&" in job and job.rstrip().endswith(LOG)]
    assert chained, "expected at least one chained job"
    for job in chained:
        command = job.rstrip()[: -len(LOG)].rstrip()
        assert command.split(None, 5)[5].startswith("{") and command.endswith("; }"), job
