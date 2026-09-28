"""Roadmap 23.9: `stonks lab verify`."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from stonks.cli import app
from tests.integration.test_cli_model_versions import cli_env  # noqa: F401


def test_verify_a_strategy_without_a_lab_run(cli_env):  # noqa: F811
    out = CliRunner().invoke(app, ["lab", "verify", "mf", "--json"], env={"COLUMNS": "250"})
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    start = lines.index("{")  # structured log lines come first
    end = len(lines) - lines[::-1].index("}")
    result = json.loads("\n".join(lines[start:end]))
    assert result["checked"] == 1 and result["moved"] == []
    assert result["reports"][0]["kind"] == "strategy"


def test_an_unknown_target_fails(cli_env):  # noqa: F811
    out = CliRunner().invoke(app, ["lab", "verify", "nope"], env={"COLUMNS": "250"})
    assert out.exit_code == 1 and "no lab run or strategy" in out.output
