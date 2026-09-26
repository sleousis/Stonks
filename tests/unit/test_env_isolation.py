"""Default test runs must not see the developer's real environment or .env:
an absolute STONKS_DATA_DIR would otherwise point CLI tests at the real
state.sqlite. Live-marked tests keep the environment so they get their keys.
"""

from __future__ import annotations

import os

import pytest

# Set at import time, i.e. before any fixture runs, to stand in for a
# variable the developer exported or a .env provided.
os.environ["STONKS_ENV_ISOLATION_SENTINEL"] = "leak"


def _leaked() -> list[str]:
    return [k for k in os.environ if k.startswith(("STONKS_", "ALPACA_")) or k == "EODHD_API_KEY"]


def test_non_live_tests_see_no_stonks_broker_or_vendor_env():
    assert _leaked() == []


def test_dotenv_loading_is_disabled_in_non_live_tests(tmp_path):
    """The CLI and API server call ``load_dotenv`` themselves, which finds
    the developer's .env by searching upward from the source tree."""
    import dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(f"STONKS_DATA_DIR={tmp_path / 'real'}\n")
    try:
        dotenv.load_dotenv(env_file)
        assert "STONKS_DATA_DIR" not in os.environ
    finally:
        os.environ.pop("STONKS_DATA_DIR", None)


@pytest.mark.live
def test_live_tests_keep_the_environment():
    assert os.environ.get("STONKS_ENV_ISOLATION_SENTINEL") == "leak"
