"""Export the OpenAPI contract the Angular client is generated from.

Regenerate after changing any route or model::

    uv run python -m stonks.api.openapi            # writes web/openapi.json
    uv run python -m stonks.api.openapi out.json   # custom path

``tests/integration/app/test_openapi_contract.py`` fails when the
checked-in file is stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from stonks.config import Settings

DEFAULT_OUTPUT = Path("web/openapi.json")


def render_openapi() -> str:
    """The contract as stable, pretty-printed JSON (sorted keys)."""
    from stonks.api.app import create_app

    # Build from pure defaults so the contract never depends on the local
    # config or environment (no stores are opened: lifespan doesn't run).
    spec = create_app(Settings()).openapi()
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_openapi(path: Path = DEFAULT_OUTPUT) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_openapi(), encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    out = write_openapi(Path(args[0]) if args else DEFAULT_OUTPUT)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
