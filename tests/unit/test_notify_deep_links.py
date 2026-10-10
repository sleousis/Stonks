"""Every deep link the notify layer can produce opens a console page.

A notification's link is opened by the service worker, the feed, email and
Telegram. A link with no console route lands on Not found. This guard reads
every ``deep_link=`` (and calendar ``link=``) the backend writes, plus the
``deep_link`` properties of alert hits, and resolves each against the
Angular routes in ``web/src/app/app.routes.ts`` and the page route files it
loads. A link expression the guard cannot read fails the run unless it is
a known pass-through (``PASS_THROUGHS``). Links stored before a page moved (``/alerts``, ``/subscriptions``,
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


#: Expressions that forward a link without writing one: ``(file, source)``.
#: Each link they carry is read where it is first written (a ``deep_link=``
#: keyword or a ``deep_link`` property). Add one only for a pure pass-through.
PASS_THROUGHS = {
    ("src/stonks/app/notifications.py", "i.deep_link"),  # feed item to the API model
    ("src/stonks/calendars/alert_kinds/base.py", "self.link"),  # set by link= in the kinds
    ("src/stonks/calendars/alerts.py", "hit.deep_link"),  # the property above
    ("src/stonks/notify/service.py", "link if isinstance(link, str) else None"),  # stored row
    ("src/stonks/notify/worker.py", "note['deep_link']"),  # the outbox row
    ("src/stonks/production/tick.py", "deep_link"),  # an argument, written by its callers
}


def _literals(node: ast.expr, constants: dict[str, str]) -> list[str] | None:
    """The links ``node`` builds, with each f-string placeholder as ``x``:
    one per branch of a conditional, none for ``None``. ``None`` when the
    guard cannot read it."""
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str)):
        return [] if node.value is None else [node.value]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("x")
        return ["".join(parts)]
    if isinstance(node, ast.Name) and node.id in constants:
        return [constants[node.id]]
    if isinstance(node, ast.IfExp):
        branches = [_literals(node.body, constants), _literals(node.orelse, constants)]
        if any(b is None for b in branches):
            return None
        return [link for b in branches if b is not None for link in b]
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


@dataclass
class LinkScan:
    #: ``{link: [where]}`` for every link the guard read.
    links: dict[str, list[str]] = field(default_factory=dict)
    #: ``(file, source)`` of every link expression it could not read.
    unread: set[tuple[str, str]] = field(default_factory=set)


def scan_links(sources: dict[str, str]) -> LinkScan:
    """Every ``deep_link=`` and ``link=`` keyword, and every return of a
    ``deep_link`` property, in ``{file: source}``."""
    scan = LinkScan()
    for where, source in sources.items():
        tree = ast.parse(source)
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            values: list[ast.expr] = []
            if isinstance(node, ast.keyword) and node.arg in ("deep_link", "link"):
                values.append(node.value)
            elif isinstance(node, ast.FunctionDef) and node.name == "deep_link":
                values += [r.value for r in ast.walk(node) if isinstance(r, ast.Return) and r.value]
            for value in values:
                links = _literals(value, constants)
                if links is None:
                    scan.unread.add((where, ast.unparse(value)))
                    continue
                for link in links:
                    if link.startswith("/"):
                        scan.links.setdefault(link, []).append(f"{where}:{value.lineno}")
    return scan


def produced_scan() -> LinkScan:
    return scan_links(
        {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(SRC.rglob("*.py"))
        }
    )


def produced_links() -> dict[str, list[str]]:
    """``{link: [where]}`` for every literal link the backend writes."""
    return produced_scan().links


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
            if not route.path and rest:
                # an empty path redirects only the bare parent (pathMatch full)
                continue
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


def test_the_guard_fails_on_a_link_it_cannot_read():
    """A link the guard cannot read is reported, never skipped: a constant
    from another module or a call. A conditional link yields each branch."""
    source = "\n".join(
        [
            "from stonks import links",
            "publish(deep_link=links.APPROVALS)",
            "publish(deep_link=page_for(kind))",
            "publish(deep_link='/alerts' if hot else '/health')",
            "publish(deep_link=None)",
        ]
    )
    scan = scan_links({"src/stonks/x.py": source})
    assert set(scan.links) == {"/alerts", "/health"}
    assert scan.unread == {
        ("src/stonks/x.py", "links.APPROVALS"),
        ("src/stonks/x.py", "page_for(kind)"),
    }


def test_every_link_the_backend_writes_is_read():
    """Only the pass-throughs below may hide a link from the guard. Each
    forwards a link that is read where it is first written."""
    assert produced_scan().unread == PASS_THROUGHS


def test_every_notification_link_opens_a_console_page(console_routes):
    broken = {
        link: where for link, where in produced_links().items() if not resolve(link, console_routes)
    }
    assert broken == {}, f"deep links with no console route: {broken}"


@pytest.mark.parametrize("link", LEGACY_LINKS)
def test_links_in_older_notifications_redirect_to_a_page(console_routes, link):
    assert resolve(link, console_routes), link
