"""Every deep link the notify layer can produce opens a console page.

A notification's link is opened by the service worker, the feed, email and
Telegram. A link with no console route lands on Not found. This guard reads
every ``deep_link=`` (and calendar ``link=``) the backend writes, plus the
``deep_link`` properties of alert hits, and resolves each against the
Angular routes in ``web/src/app/app.routes.ts`` and the page route files it
loads. Links stored before a page moved (``/alerts``, ``/subscriptions``,
``/approvals``, ``/signals``) must still resolve through a console redirect.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "stonks"
APP = ROOT / "web" / "src" / "app"

#: Links older notifications carry. Each must redirect to a real page.
LEGACY_LINKS = ("/alerts", "/subscriptions", "/approvals", "/signals?strategy=s1")


# ---- the links the backend writes ---------------------------------------------------


def _literal(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The link ``node`` builds, with each f-string placeholder as ``x``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("x")
        return "".join(parts)
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                out[target.id] = value.value
    return out


def produced_links() -> dict[str, list[str]]:
    """``{link: [where]}`` for every literal link the backend writes."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        where = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            values: list[ast.expr] = []
            if isinstance(node, ast.keyword) and node.arg in ("deep_link", "link"):
                values.append(node.value)
            elif isinstance(node, ast.FunctionDef) and node.name == "deep_link":
                values += [r.value for r in ast.walk(node) if isinstance(r, ast.Return) and r.value]
            for value in values:
                link = _literal(value, constants)
                if link is not None and link.startswith("/"):
                    found.setdefault(link, []).append(f"{where}:{value.lineno}")
    return found


# ---- the console's routes -----------------------------------------------------------


@dataclass
class Route:
    path: str
    children: list[Route] = field(default_factory=list)
    #: A string redirect target, or ``"<fn>"`` for a function redirect.
    redirect: str | None = None
    leaf: bool = True


_PATH = re.compile(r"path:\s*'([^']*)'")
_REDIRECT = re.compile(r"redirectTo:\s*(?:'([^']*)'|(\())")
_COMMENT = re.compile(r"/\*.*?\*/|(?<![:'])//[^\n]*", re.S)
_LOAD_CHILDREN = re.compile(r"loadChildren:\s*\(\)\s*=>\s*import\('([^']+)'\)")


def _skip_string(text: str, i: int) -> int:
    """The index just past the string literal that starts at ``i``."""
    return text.index(text[i], i + 1) + 1


def _objects(text: str, start: int) -> list[str]:
    """The ``{...}`` objects of the route array whose ``[`` is at ``start``."""
    out, depth, begin = [], 0, 0
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch in "'`":
            i = _skip_string(text, i)
            continue
        if ch in "{[":
            if depth == 0 and ch == "{":
                begin = i
            depth += 1
        elif ch in "}]":
            if depth == 0:
                break
            depth -= 1
            if depth == 0 and ch == "}":
                out.append(text[begin : i + 1])
        i += 1
    return out


def _shallow(obj: str) -> str:
    """``obj`` without its nested objects and arrays (its own keys only)."""
    out, depth, i = [], 0, 0
    while i < len(obj):
        ch = obj[i]
        if ch in "'`":
            end = _skip_string(obj, i)
            if depth <= 1:
                out.append(obj[i:end])
            i = end
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif depth <= 1:
            out.append(ch)
        i += 1
    return "".join(out)


def _routes(text: str, start: int, base: Path) -> list[Route]:
    routes = []
    for obj in _objects(text, start):
        own = _shallow(obj)
        path = _PATH.search(own)
        if path is None:
            continue
        route = Route(path.group(1))
        redirect = _REDIRECT.search(own)
        lazy = _LOAD_CHILDREN.search(own)
        if redirect is not None:
            route.redirect = redirect.group(1) if redirect.group(1) is not None else "<fn>"
        elif lazy is not None:
            route.children = _route_file(base / f"{lazy.group(1)}.ts")
            route.leaf = False
        elif "children:" in own:
            route.children = _routes(obj, obj.index("[", obj.index("children:")), base)
            route.leaf = False
        routes.append(route)
    return routes


def _route_file(file: Path) -> list[Route]:
    text = _COMMENT.sub("", file.read_text(encoding="utf-8"))
    marker = re.search(r"(=|default)\s*\[", text)
    assert marker is not None, file
    return _routes(text, marker.end() - 1, file.parent)


def _matches(pattern: str, segments: list[str]) -> list[str] | None:
    parts = [p for p in pattern.split("/") if p]
    if len(parts) > len(segments):
        return None
    for part, seg in zip(parts, segments, strict=False):
        if not part.startswith(":") and part != seg:
            return None
    return segments[len(parts) :]


def resolve(link: str, routes: list[Route], depth: int = 0) -> bool:
    """Whether ``link`` reaches a page (following string redirects)."""
    if depth > 5:
        return False
    path = re.split(r"[?#]", link, maxsplit=1)[0]
    return _resolve([s for s in path.split("/") if s], routes, routes, depth)


def _resolve(segments: list[str], routes: list[Route], top: list[Route], depth: int) -> bool:
    for route in routes:
        if route.path == "**":
            continue
        rest = _matches(route.path, segments)
        if rest is None:
            continue
        if route.redirect is not None:
            if route.redirect == "<fn>":
                return True
            target = route.redirect if route.redirect.startswith("/") else f"/{route.redirect}"
            return resolve(target, top, depth + 1)
        if not rest and route.leaf:
            return True
        if route.children and _resolve(rest, route.children, top, depth):
            return True
    return False


@pytest.fixture(scope="module")
def console_routes() -> list[Route]:
    return _route_file(APP / "app.routes.ts")


def test_the_guard_reads_links_and_routes(console_routes):
    links = produced_links()
    assert "/notifications" in links and "/health" in links
    assert any(link.startswith("/strategies/") for link in links)
    assert resolve("/strategies/abc", console_routes)
    assert resolve("/orders/ticks/t1", console_routes)
    assert not resolve("/no-such-page", console_routes)
    assert not resolve("/orders/no-such-tab", console_routes)


def test_every_notification_link_opens_a_console_page(console_routes):
    broken = {
        link: where for link, where in produced_links().items() if not resolve(link, console_routes)
    }
    assert broken == {}, f"deep links with no console route: {broken}"


@pytest.mark.parametrize("link", LEGACY_LINKS)
def test_links_in_older_notifications_redirect_to_a_page(console_routes, link):
    assert resolve(link, console_routes), link
