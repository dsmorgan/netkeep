"""The attach path against a real Chrome. Opt in with ``NETKEEP_BROWSER_TESTS=1``.

Skipped by default, because it needs a browser the developer started and the rest of
the suite must stay offline. What it drives is a loopback HTTP server in this
process: the only site involved is one this file serves, and nothing here knows a
LinkedIn URL. CONTRIBUTING.md has the two commands to run it.

What it proves that the offline tests cannot: that the narrow protocols in
``netkeep.linkedin.browser`` match what Playwright actually does, that the tab
netkeep opens sends the user's own headers and client hints because nothing
overrides them, that a tab closed by hand comes back where it was, and that the whole
run adds no browser process and no context.

Start Chrome first with the command ``netkeep browser launch`` prints.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from browser_guard import isolated_cdp

from netkeep.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeep.linkedin.preflight import LoginState, preflight

pytestmark = pytest.mark.skipif(
    os.environ.get("NETKEEP_BROWSER_TESTS") != "1",
    reason="browser smoke suite: start Chrome, then set NETKEEP_BROWSER_TESTS=1",
)

CDP_URL = os.environ.get("NETKEEP_CDP_URL", "http://127.0.0.1:9222")

PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>netkeep smoke replica</title></head>
<body><h1 id="marker">netkeep smoke replica</h1>
<div style="height:4000px">a page tall enough to scroll</div></body></html>
"""


class Replica(BaseHTTPRequestHandler):
    """A two-page site on loopback: one page to sit on, one that echoes the headers."""

    def do_GET(self) -> None:
        if self.path.startswith("/headers"):
            payload = {name.lower(): value for name, value in self.headers.items()}
            body = json.dumps(payload).encode()
            content_type = "text/plain; charset=utf-8"
        else:
            body = PAGE
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the request log out of the test output."""


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    """The loopback site's base URL, served for the length of the module."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Replica)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def provider() -> AttachBrowserProvider:
    return AttachBrowserProvider(CDP_URL)


def browser_processes() -> int | None:
    """How many browser processes are running, or None when we cannot tell."""
    name = "Google Chrome" if sys.platform == "darwin" else "chrome"
    try:
        found = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return len([line for line in found.stdout.splitlines() if line.strip()])


async def test_attach_reuses_the_running_browser(provider: AttachBrowserProvider) -> None:
    try:
        async with provider.run() as run:
            assert run.browser.is_connected()
            assert run.browser.version
            assert len(run.browser.contexts) >= 1
            assert run.context is run.browser.contexts[0]
            page = await run.ensure_page()
            assert not page.is_closed()
    except BrowserUnavailable as exc:
        pytest.fail(f"{exc}\nStart Chrome with the command `netkeep browser launch` prints.")


async def test_the_tab_sends_the_browsers_own_headers(
    provider: AttachBrowserProvider, site: str
) -> None:
    """Nothing overrides the UA or the client hints, so the tab looks like the user's."""
    async with provider.run() as run:
        page = await run.goto(f"{site}/headers")
        sent = json.loads(await page.evaluate("() => document.body.textContent"))
        fingerprint_ua = await page.evaluate("() => navigator.userAgent")

    assert "Chrome" in sent["user-agent"], sent["user-agent"]
    assert sent["user-agent"] == fingerprint_ua, "the request UA is not the browser's own"
    assert "Headless" not in sent["user-agent"], "run the smoke suite against a windowed Chrome"
    assert sent.get("accept-language"), "the profile's languages did not reach the request"
    assert sent.get("sec-ch-ua"), "Chrome's client hints did not reach the request"
    assert sent.get("sec-ch-ua-platform")


async def test_a_tab_closed_by_hand_comes_back_where_it_was(
    provider: AttachBrowserProvider, site: str
) -> None:
    async with provider.run() as run:
        page = await run.goto(f"{site}/")
        await page.close()  # the user closing netkeep's tab
        assert page.is_closed()

        recovered = await run.ensure_page()

        assert not recovered.is_closed()
        assert recovered is not page
        assert recovered.url.rstrip("/") == site
        assert await recovered.evaluate("() => document.title") == "netkeep smoke replica"


async def test_a_run_adds_no_browser_and_no_context(
    provider: AttachBrowserProvider, site: str
) -> None:
    """ADR 0002 from the outside: the run leaves the browser exactly as it found it."""
    before = browser_processes()
    async with provider.run() as run:
        contexts_before = len(run.browser.contexts)
        pages_before = len(run.context.pages)
        await run.goto(f"{site}/")
        assert len(run.browser.contexts) == contexts_before, "a second context appeared"

    async with provider.run() as run:
        assert len(run.browser.contexts) == contexts_before
        await run.ensure_page()
        assert len(run.context.pages) == pages_before + 1

    after = browser_processes()
    if before is not None and after is not None:
        assert after <= before, f"browser processes went from {before} to {after}"
    assert not hasattr(provider, "launch")


#: #819: what a tab reports for the dark color scheme.
PREFERS_DARK = "() => matchMedia('(prefers-color-scheme: dark)').matches"


@asynccontextmanager
async def _tabs_without_emulation() -> AsyncIterator[Any]:
    """Opens tabs the way a person or the UI does, through a test-only connection that
    emulates nothing (``no_defaults``), so they report the system's own color scheme."""
    from playwright.async_api import async_playwright

    # A raw connect skips the guarded connector: refuse a personal Chrome first.
    cdp_url = isolated_cdp(CDP_URL)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url, no_defaults=True)
        opened: list[Any] = []

        async def open_tab(url: str) -> Any:
            page = await browser.contexts[0].new_page()
            opened.append(page)
            await page.goto(url)
            return page

        try:
            yield open_tab
        finally:
            for page in opened:
                if not page.is_closed():
                    await page.close()
            await browser.close()


async def test_attached_tabs_keep_the_systems_color_scheme(
    provider: AttachBrowserProvider, site: str
) -> None:
    """#819: during a run, the UI's tab, a tab opened mid-run, and the run's own tab all
    report the system's color scheme, not Playwright's light default. Only a dark system
    tells the two apart, so a light one skips."""
    async with _tabs_without_emulation() as open_tab:
        ui = await open_tab(f"{site}/")
        if not await ui.evaluate(PREFERS_DARK):
            pytest.skip("the system theme is light, Playwright's default too: set it to dark")
        async with provider.run() as run:
            page = await run.goto(f"{site}/")
            assert await page.evaluate(PREFERS_DARK), "the run's tab turned light"
            assert await ui.evaluate(PREFERS_DARK), "a tab open before the attach turned light"
            later = await open_tab(f"{site}/")
            assert await later.evaluate(PREFERS_DARK), "a tab opened mid-run turned light"
        assert await ui.evaluate(PREFERS_DARK), "the tab stayed light after the run"


async def test_preflight_reads_the_real_profile(provider: AttachBrowserProvider) -> None:
    report = await preflight(provider)

    assert report.attached, report.problems
    assert report.browser_version
    assert report.fingerprint is not None
    assert report.fingerprint.user_agent
    assert report.fingerprint.timezone
    assert not report.fingerprint.webdriver, "start Chrome without automation flags"
    assert report.login in (LoginState.LOGGED_IN, LoginState.NO_SESSION)
    if report.login is LoginState.LOGGED_IN:
        assert "li_at" in report.session_cookies
    # Names, never values: the report carries nothing a log could leak.
    assert all(len(name) < 40 for name in report.session_cookies)
