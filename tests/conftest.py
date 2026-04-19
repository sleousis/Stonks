"""Load a developer ``.env`` (if present) into the process environment before
any test module is imported. Without this, the live-API gates — which
read ``os.environ`` at module-decoration time — miss a key that lives only
in ``.env``, and ``STONKS_RUN_LIVE_TESTS=1 pytest`` silently skips.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# The .env at the repo root, relative to this conftest (tests/conftest.py).
_ENV = Path(__file__).parent.parent / ".env"
if _ENV.is_file():
    load_dotenv(_ENV, override=False)
