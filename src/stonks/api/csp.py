"""Content-Security-Policy and the basic security headers.

The console loads everything from its own origin: its scripts, styles and
fonts (bundled with fontsource), the REST API, the SSE job streams and the
service worker that receives web push. Its index.html also has two small
inline scripts (the theme before first paint and the route preload loop).
The policy allows exactly those two by their sha256 hash, so no other
inline script can run, even if one were injected into the page.

When an inline script in ``web/src/index.html`` changes, run
``uv run python -m tools.csp_hashes`` and copy the hashes it prints into
:data:`CONSOLE_SCRIPT_HASHES` and the ``Content-Security-Policy`` line of
``deploy/Caddyfile``. ``tests/unit/test_api_csp.py`` fails until both match.

``stonks serve`` sets these headers itself, so local use is covered too.
Behind Caddy the app's headers win: Caddy only fills them in where a
response has none (its own error pages, for example).
"""

from __future__ import annotations

import base64
import hashlib
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: sha256 hashes of the inline scripts in web/src/index.html, in page order.
CONSOLE_SCRIPT_HASHES: tuple[str, ...] = (
    "sha256-KC3V0qVKo5qInDk2JVN6KUjFYu6/nAE0F9WvsYC53lI=",
    "sha256-mkNkfJL6Td2NlcQandHgX945couk/OUEYLKTr/ymsNU=",
)

#: FastAPI's interactive API docs load Swagger UI and ReDoc from this CDN,
#: with an inline start script. ReDoc also loads its fonts from Google
#: Fonts (a stylesheet, then the font files). Only those pages get the
#: looser policy.
_DOCS_CDN = "https://cdn.jsdelivr.net"
_DOCS_FONT_CSS = "https://fonts.googleapis.com"
_DOCS_FONTS = "https://fonts.gstatic.com"
_DOCS_PATHS = ("/docs", "/redoc")

_COMMON = (
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)


def console_policy() -> str:
    """The policy of the console and of every API response."""
    scripts = " ".join(f"'{h}'" for h in CONSOLE_SCRIPT_HASHES)
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' {scripts}",
            # Angular adds component styles as <style> elements and binds
            # style attributes, so inline styles stay allowed. Styles cannot
            # run code.
            "style-src 'self' 'unsafe-inline'",
            # data: for small inline images (the 2FA QR code), blob: for
            # files the console builds for download.
            "img-src 'self' data: blob:",
            "font-src 'self'",
            # The API, the SSE job streams and push subscriptions.
            "connect-src 'self'",
            "worker-src 'self'",
            "manifest-src 'self'",
            *_COMMON,
        )
    )


def docs_policy() -> str:
    """The policy of the interactive API docs (/docs, /redoc)."""
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'unsafe-inline' {_DOCS_CDN}",
            f"style-src 'self' 'unsafe-inline' {_DOCS_CDN} {_DOCS_FONT_CSS}",
            "img-src 'self' data: https://fastapi.tiangolo.com",
            f"font-src 'self' {_DOCS_CDN} {_DOCS_FONTS}",
            "connect-src 'self'",
            "worker-src 'self' blob:",
            *_COMMON,
        )
    )


_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\b[^>]*>", re.S | re.I)
_EXECUTABLE_TYPES = {"", "text/javascript", "application/javascript", "module"}


def _attribute(attrs: str, name: str) -> str | None:
    match = re.search(rf"\b{name}\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", attrs, re.I)
    return match.group(1).strip("\"'") if match else None


def inline_script_hashes(html: str) -> list[str]:
    """``sha256-...`` sources for each inline script the browser would run.

    External scripts (``src=``) and data blocks (``type="application/json"``)
    are skipped: script-src does not apply to their text.
    """
    found = []
    for match in _SCRIPT.finditer(html):
        attrs, body = match.group(1), match.group(2)
        if _attribute(attrs, "src") is not None:
            continue
        if (_attribute(attrs, "type") or "").lower() not in _EXECUTABLE_TYPES:
            continue
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        found.append("sha256-" + base64.b64encode(digest).decode("ascii"))
    return found


_DEFAULT_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)


class SecurityHeadersMiddleware:
    """Adds the CSP and the basic security headers to every HTTP response.

    Pure ASGI, so streamed SSE responses pass through untouched. A header a
    route set itself is kept.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._console = console_policy().encode("ascii")
        self._docs = docs_policy().encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_docs = any(path == p or path.startswith(p + "/") for p in _DOCS_PATHS)
        policy = self._docs if is_docs else self._console

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra = [(b"content-security-policy", policy), *_DEFAULT_HEADERS]
                headers.extend((n, v) for n, v in extra if n not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
