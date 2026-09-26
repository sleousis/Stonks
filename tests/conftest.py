"""Keep default test runs isolated from the developer's environment.

- The developer ``.env`` is loaded into the process environment only for
  live runs (``STONKS_RUN_LIVE_TESTS=1``, exported or set in ``.env``), and
  before any test module is imported: the live-API gates read ``os.environ``
  at module-decoration time.
- Every test not marked ``live`` runs with ``STONKS_*``, ``ALPACA_*`` and
  ``EODHD_API_KEY`` removed and ``dotenv.load_dotenv`` disabled (the CLI
  and API server call it themselves), so an absolute ``STONKS_DATA_DIR`` or
  a real broker key can never reach a hermetic test. Tests that need one
  set it with ``monkeypatch.setenv``.
- CLI output is plain text everywhere. Typer forces colored output when it
  sees ``GITHUB_ACTIONS`` and decides at import time, so colors are turned
  off here, before any test imports the CLI.
"""

from __future__ import annotations

import os
from pathlib import Path

import dotenv
import pytest

_LIVE_FLAG = "STONKS_RUN_LIVE_TESTS"

# Plain CLI output on every machine, including CI (see module docstring).
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["NO_COLOR"] = "1"

# The .env at the repo root, relative to this conftest (tests/conftest.py).
_ENV = Path(__file__).parent.parent / ".env"
if _ENV.is_file() and "1" in (
    os.environ.get(_LIVE_FLAG),
    dotenv.dotenv_values(_ENV).get(_LIVE_FLAG),
):
    dotenv.load_dotenv(_ENV, override=False)


def _is_isolated_var(name: str) -> bool:
    return name.startswith(("STONKS_", "ALPACA_")) or name == "EODHD_API_KEY"


@pytest.fixture(autouse=True)
def _isolate_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live") is not None:
        return
    for name in [n for n in os.environ if _is_isolated_var(n)]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
