"""``stonks serve`` sends the Content-Security-Policy itself, so the console
is covered without Caddy too (local use)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.api.csp import CONSOLE_SCRIPT_HASHES, console_policy, docs_policy
from tests.integration.app.test_api import LOOPBACK


def _app(settings, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><script>theme()</script>app</html>")
    settings.api.ui_dist = dist
    settings.api.allowed_hosts = ["testserver"]
    return create_app(settings)


def test_the_console_page_carries_the_policy_with_the_inline_script_hashes(
    settings, seeded, tmp_path
):
    with TestClient(_app(settings, tmp_path), client=LOOPBACK) as c:
        for path in ("/", "/orders/123"):
            resp = c.get(path)
            assert resp.status_code == 200, path
            policy = resp.headers["content-security-policy"]
            assert policy == console_policy(), path
            for digest in CONSOLE_SCRIPT_HASHES:
                assert f"'{digest}'" in policy
            assert resp.headers["x-content-type-options"] == "nosniff"


def test_api_responses_carry_it_too(settings, seeded, tmp_path):
    with TestClient(_app(settings, tmp_path), client=LOOPBACK) as c:
        resp = c.get("/api/health")
        assert resp.headers["content-security-policy"] == console_policy()
        # A refused Host gets the headers as well.
        refused = c.get("/api/health", headers={"Host": "evil.example"})
        assert refused.status_code == 400
        assert refused.headers["content-security-policy"] == console_policy()


def test_the_api_docs_get_the_docs_policy(settings, seeded, tmp_path):
    with TestClient(_app(settings, tmp_path), client=LOOPBACK) as c:
        resp = c.get("/docs")
        assert resp.status_code == 200
        assert resp.headers["content-security-policy"] == docs_policy()
