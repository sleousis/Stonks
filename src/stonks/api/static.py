"""Serve the built UI (``web/dist``) at ``/`` with single-page-app fallback."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


def mount_spa(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()
    index = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        if full_path:
            candidate = (root / full_path).resolve()
            # resolve() collapses ``..`` and symlinks; anything outside the
            # dist root falls through to index.html instead of being served.
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
        if not index.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(index)
