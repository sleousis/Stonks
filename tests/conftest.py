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
- No network (TT-05): ``pytest-socket`` lets sockets connect only to
  loopback (``--allow-hosts`` in ``pyproject.toml``). Tests marked ``live``
  get the network back.
- CLI output is plain text everywhere. Typer forces colored output when it
  sees ``GITHUB_ACTIONS`` and decides at import time, so colors are turned
  off here, before any test imports the CLI.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import dotenv
import pytest

from stonks.store.lake import DuckDBLake

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


def enable_network_for_live_tests(items) -> None:
    """Give tests marked ``live`` the network; every other test may only
    connect to loopback."""
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(pytest.mark.enable_socket)


@pytest.fixture
def lake(tmp_path: Path) -> Iterator[DuckDBLake]:
    """An empty, migrated lake in the test's temp folder (TT-09). A test
    module that needs seeded data or another bar backend overrides it."""
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    yield db
    db.close()


# Test-run switches, not app settings: they must survive the isolation.
_TEST_SWITCHES = frozenset({"STONKS_UPDATE_GOLDEN", "STONKS_RUN_LIVE_TESTS"})


def _is_isolated_var(name: str) -> bool:
    if name in _TEST_SWITCHES:
        return False
    return name.startswith(("STONKS_", "ALPACA_")) or name == "EODHD_API_KEY"


@pytest.fixture(autouse=True)
def _isolate_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live") is not None:
        return
    for name in [n for n in os.environ if _is_isolated_var(n)]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    # Lab survival tests run their backtests in-process unless a test asks
    # for a pool (``max_workers=...``): the suite already runs in parallel,
    # and in-process runs keep spy strategies' class-level records visible.
    # (Patched on the module, not through STONKS_LAB_MAX_WORKERS, which the
    # isolation above must strip like every STONKS_ variable.)
    monkeypatch.setattr("stonks.lab.parallel.default_max_workers", lambda: 1)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Live tests get the network back (TT-05). Browser journeys (``e2e``)
    run only when the marker expression names them (``-m e2e``), so the
    default run stays fast and needs no browser."""
    enable_network_for_live_tests(items)
    if "e2e" in (config.option.markexpr or ""):
        return
    selected, deselected = [], []
    for item in items:
        (deselected if item.get_closest_marker("e2e") else selected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
