"""Fixtures for the browser journeys: one seeded stack per session, a fresh
browser context per person, and the checks every page must pass.

- ``stack``: :mod:`tests.e2e.stack` built in a temp folder, ``stonks serve``
  running on a free port with the built console.
- ``viewport``: every journey runs twice, ``desktop`` (1280x800) and
  ``phone`` (375x812).
- ``browse``: opens a context for a person (signed in, or not), traced and
  watched by a :class:`Guard`. When the test fails the trace goes to
  ``test-results/<test>/``.
- :class:`Guard` fails the test on a console error or a failed request that
  the test did not expect.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, BrowserContext, ConsoleMessage, Page, Response, expect

from tests.e2e.stack import DEFAULT_DIST, Person, Stack, build_stack, start_server, stop_server

VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "phone": {"width": 375, "height": 812},
}
RESULTS_DIR = Path(os.environ.get("STONKS_E2E_RESULTS", "test-results"))
AXE_REPORT = RESULTS_DIR / "axe-report.json"

expect.set_options(timeout=15_000)


@dataclass(frozen=True)
class Refusal:
    status: int
    path: str  # regex on the URL path
    why: str

    def matches(self, status: int, url: str) -> bool:
        return status == self.status and re.search(self.path, url) is not None


#: Failed requests the console makes today that are not part of any journey.
#: Each one is an app issue listed in docs/testing.md; remove it once fixed.
KNOWN_NOISE: tuple[Refusal, ...] = (
    Refusal(401, r"/api/auth/me$", "signed out: the console asks who is signed in"),
    Refusal(401, r"/api/(halts|schedule|strategies)\b", "BUG-1: shell polls while signed out"),
    Refusal(404, r"/api/subscriptions$", "BUG-2: no /api/subscriptions route yet"),
)


#: axe violations the console has today, by (page path, rule). Each is an app
#: fix listed in docs/testing.md; a strict xfail keeps it visible until fixed.
KNOWN_AXE: dict[tuple[str, str], str] = {
    ("/settings", "landmark-unique"): (
        "A11Y-1: the Notifications panel on Settings has the same name as the toast region"
    ),
    ("/universes", "landmark-unique"): (
        "A11Y-2: the data table's scroll region repeats its panel's name (Stored universes)"
    ),
    ("/ops/halts", "landmark-unique"): (
        "A11Y-2: the data table's scroll region repeats its panel's name (Active halts)"
    ),
}


def new_axe_violations(page: Page, violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = "/" + page.url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    return [v for v in violations if (path, v["id"]) not in KNOWN_AXE]


@dataclass
class Guard:
    """Collects console errors and failed requests for one browser context."""

    allowed: list[Refusal] = field(default_factory=lambda: list(KNOWN_NOISE))
    problems: list[str] = field(default_factory=list)
    seen_refusals: list[str] = field(default_factory=list)

    def expect_refusal(self, status: int, path: str, why: str = "expected") -> None:
        self.allowed.append(Refusal(status, path, why))

    def on_console(self, msg: ConsoleMessage) -> None:
        if msg.type != "error":
            return
        # The browser logs every 4xx/5xx as "Failed to load resource"; the
        # response hook judges those against the allowed refusals.
        if msg.text.startswith("Failed to load resource"):
            return
        self.problems.append(f"console error: {msg.text}")

    def on_response(self, resp: Response) -> None:
        if resp.status < 400:
            return
        url = resp.url.split("?", 1)[0]
        for refusal in self.allowed:
            if refusal.matches(resp.status, url):
                self.seen_refusals.append(f"{resp.status} {url}")
                return
        self.problems.append(f"failed request: {resp.status} {resp.request.method} {resp.url}")

    def on_page_error(self, error: Exception) -> None:
        self.problems.append(f"page error: {error}")

    def attach(self, context: BrowserContext) -> None:
        context.on("console", self.on_console)
        context.on("response", self.on_response)
        context.on("weberror", lambda e: self.on_page_error(e.error))

    def assert_clean(self) -> None:
        assert not self.problems, "\n".join(self.problems)


# ---- stack ------------------------------------------------------------------


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    dist = Path(os.environ.get("STONKS_E2E_DIST", DEFAULT_DIST))
    if not (dist / "index.html").is_file():
        pytest.fail(f"no built console at {dist}: run `npm ci && npm run build` in web/")
    built = build_stack(tmp_path_factory.mktemp("stack"), dist=dist)
    start_server(built)
    try:
        yield built
    finally:
        stop_server(built)
        if built.log_path is not None:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            (RESULTS_DIR / "server.log").write_bytes(built.log_path.read_bytes())


@pytest.fixture(params=list(VIEWPORTS))
def viewport(request: pytest.FixtureRequest) -> str:
    return request.param


# ---- failure artifacts --------------------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def _slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", nodeid.split("::", 1)[-1]).strip("-")[:120]


# ---- browsing ---------------------------------------------------------------


@dataclass
class Visit:
    """A signed-in (or signed-out) person in their own browser context."""

    stack: Stack
    context: BrowserContext
    page: Page
    guard: Guard
    viewport: str
    person: Person | None = None

    @property
    def phone(self) -> bool:
        return self.viewport == "phone"

    def go(self, path: str) -> Page:
        self.page.goto(self.stack.base_url + path)
        return self.page

    def sign_in(self, person: Person) -> None:
        """Password, then a fresh TOTP code, landing on home."""
        page = self.go("/login")
        page.get_by_label("Email").fill(person.email)
        page.get_by_label("Password").fill(person.password)
        page.get_by_role("button", name="Sign in").click()
        page.get_by_role("textbox", name="Code").fill(person.code())
        page.get_by_role("button", name="Continue").click()
        expect(page.get_by_role("heading", level=1)).to_contain_text("Hello")
        self.person = person

    def api(self, method: str, path: str, **kwargs: Any) -> Response:
        """A call with this context's cookies, like the console makes."""
        req = self.context.request
        headers = dict(kwargs.pop("headers", {}))
        if method.upper() != "GET":
            csrf = next(
                (c["value"] for c in self.context.cookies() if c["name"] == "stonks_csrf"), None
            )
            if csrf:
                headers["X-CSRF-Token"] = csrf
        return req.fetch(self.stack.base_url + path, method=method, headers=headers, **kwargs)

    def open_nav(self) -> None:
        """On phones the navigation sits behind the menu button."""
        if self.phone:
            self.page.get_by_role("button", name="Open navigation").click()

    def check_page(self, name: str) -> None:
        """What every page must pass: no sideways scroll on phones and no
        axe-core violation (findings also go to the report)."""
        page = self.page
        page.wait_for_load_state("networkidle")
        if self.phone:
            widths = page.evaluate(
                "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
            )
            assert widths[0] <= widths[1], f"{name}: page scrolls sideways on phone {widths}"
        violations = new_axe_violations(page, record_axe(page, name, self.viewport))
        assert not violations, f"{name}: axe violations {[v['id'] for v in violations]}"


_AXE = Axe()


def run_axe(page: Page) -> list[dict[str, Any]]:
    results = _AXE.run(page)
    return [
        {
            "id": v["id"],
            "impact": v.get("impact"),
            "help": v["help"],
            "nodes": [n.get("target") for n in v.get("nodes", [])][:5],
        }
        for v in results.response.get("violations", [])
    ]


def record_axe(page: Page, name: str, viewport: str) -> list[dict[str, Any]]:
    violations = run_axe(page)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    if AXE_REPORT.exists():
        try:
            report = json.loads(AXE_REPORT.read_text(encoding="utf-8"))
        except ValueError:
            report = {}
    report[f"{name} [{viewport}]"] = violations
    AXE_REPORT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return violations


@pytest.fixture
def browse(
    request: pytest.FixtureRequest, browser: Browser, stack: Stack, viewport: str
) -> Iterator[Callable[..., Visit]]:
    """``browse(person=None)``: a new context, signed in when ``person`` is given."""
    opened: list[Visit] = []

    def open_visit(person: Person | None = None) -> Visit:
        context = browser.new_context(
            viewport=VIEWPORTS[viewport],
            is_mobile=viewport == "phone",
            has_touch=viewport == "phone",
            base_url=stack.base_url,
            service_workers="block",
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        guard = Guard()
        guard.attach(context)
        visit = Visit(stack, context, context.new_page(), guard, viewport)
        opened.append(visit)
        if person is not None:
            visit.sign_in(person)
        return visit

    yield open_visit

    reports = [getattr(request.node, f"rep_{when}", None) for when in ("setup", "call")]
    failed = any(r is not None and (r.failed or hasattr(r, "wasxfail")) for r in reports)
    out = RESULTS_DIR / _slug(request.node.nodeid)
    for i, visit in enumerate(opened):
        if failed:
            out.mkdir(parents=True, exist_ok=True)
            visit.context.tracing.stop(path=str(out / f"trace-{i}.zip"))
        else:
            visit.context.tracing.stop()
        visit.context.close()
    if not failed:
        for visit in opened:
            visit.guard.assert_clean()
