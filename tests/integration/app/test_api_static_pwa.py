"""Serving the built console as a PWA: the service worker and its manifest
must never be cached by the browser (or updates stall), and the web app
manifest needs its registered media type."""

from __future__ import annotations

from fastapi.testclient import TestClient

from stonks.api import create_app
from tests.integration.app.test_api import LOOPBACK


def _app(settings, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "ngsw-worker.js").write_text("self.addEventListener('fetch', () => {});")
    (dist / "ngsw.json").write_text('{"configVersion": 1}')
    (dist / "manifest.webmanifest").write_text('{"name": "Stonks"}')
    (dist / "assets" / "main.js").write_text("console.log(1)")
    settings.api.ui_dist = dist
    settings.api.allowed_hosts = ["testserver"]
    return create_app(settings)


def test_service_worker_files_are_not_cached(settings, seeded, tmp_path):
    with TestClient(_app(settings, tmp_path), client=LOOPBACK) as c:
        for path in ("/ngsw-worker.js", "/ngsw.json"):
            resp = c.get(path)
            assert resp.status_code == 200, path
            assert resp.headers["cache-control"] == "no-cache", path
        assert "no-cache" not in c.get("/assets/main.js").headers.get("cache-control", "")


def test_webmanifest_media_type(settings, seeded, tmp_path):
    with TestClient(_app(settings, tmp_path), client=LOOPBACK) as c:
        resp = c.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
