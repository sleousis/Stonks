"""Serve the built UI (``web/dist``) at ``/`` with single-page-app fallback.

PWA details: the Angular service worker (``ngsw-worker.js``) and its
manifest (``ngsw.json``) go out with ``Cache-Control: no-cache``, so a new
deployment is picked up on the next check instead of whenever the HTTP
cache expires; ``*.webmanifest`` gets its registered media type whatever
the platform's MIME table says.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

#: Files the browser must revalidate on every fetch.
NO_CACHE_FILES = frozenset({"ngsw-worker.js", "ngsw.json"})
#: Media types by suffix that platform MIME tables often lack.
MEDIA_TYPES = {".webmanifest": "application/manifest+json"}


def _file_response(path: Path) -> FileResponse:
    headers = {"Cache-Control": "no-cache"} if path.name in NO_CACHE_FILES else None
    return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix), headers=headers)


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
                return _file_response(candidate)
        if not index.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(index)
