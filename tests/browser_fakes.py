"""A browser that is not a browser: fakes for the attach path's offline tests.

The real thing needs a Chrome the developer started, so everything netkeep does to
a browser goes through the narrow protocols in ``netkeep.linkedin.browser``, and
these fakes implement them. mypy checks the fakes against the same protocols the
Playwright objects satisfy, so a fake that drifts from the real API is a type error.

The fakes also carry the invariants: :meth:`FakeBrowser.new_context` and
:meth:`FakeBrowser.close` raise rather than pretend, so a code path that reaches for
a second identity or shuts the user's browser fails the test that runs it, not a
review three weeks later.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from netkeep.chrome_profile import SINGLETON_LOCK
from netkeep.linkedin.browser import CHROME_PROFILE_DIRNAME, Connection, ContextLike, PageLike
from netkeep.paths import data_dir, default_data_dir

#: The pid every fake Chrome names as its browser process, unless a test says otherwise.
FAKE_CHROME_PID = 4242


def hold_profile(profile: Path, pid: int = FAKE_CHROME_PID) -> None:
    """Make ``profile`` look held by browser process ``pid``, as Chrome's lock does.

    Never under the real data directory: a fake built before a test set
    ``NETKEEP_DATA`` would otherwise write a lock into the real profile.
    """
    if profile.resolve().is_relative_to(default_data_dir().resolve()):
        raise AssertionError(f"a fake Chrome tried to hold a real profile: {profile}")
    profile.mkdir(parents=True, exist_ok=True)
    lock = profile / SINGLETON_LOCK
    if lock.is_symlink() or lock.exists():
        lock.unlink()
    os.symlink(f"quorbelle-host-{pid}", lock)


class FakeMouse:
    """Records the wheel events a scroll plan sends, and the moves that rest the
    pointer over content before one (#192)."""

    def __init__(self) -> None:
        self.wheels: list[tuple[float, float]] = []
        self.moves: list[tuple[float, float]] = []

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self.wheels.append((delta_x, delta_y))

    async def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))


class FakeLocator:
    """A ``page.locator(selector).first`` fake: its box, armed by the page (#192).

    ``first`` returns ``self``, matching a real chained locator's own type; a
    fake never needs to represent "more than one match" to answer
    ``bounding_box``.
    """

    def __init__(self, page: FakePage, selector: str, index: int = 0) -> None:
        self._page = page
        self.selector = selector
        self._index = index

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(self._page, self.selector, index)

    async def count(self) -> int:
        listed = self._page.match_boxes.get(self.selector)
        if listed is not None:
            return len(listed)
        return 1 if self.selector in self._page.content_boxes else 0

    async def bounding_box(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- mirrors Playwright's own signature
    ) -> Mapping[str, float] | None:
        if self._page.locator_error is not None:
            error, self._page.locator_error = self._page.locator_error, None
            raise error
        listed = self._page.match_boxes.get(self.selector)
        if listed is not None:
            return listed[self._index] if self._index < len(listed) else None
        return self._page.content_boxes.get(self.selector) if self._index == 0 else None


class FakePage:
    """One tab. Records what it was asked to do and can be closed behind the run's back."""

    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.mouse = FakeMouse()
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[str] = []
        self.close_calls = 0
        self._closed = False
        self._url = "about:blank"
        self._goto_error: Exception | None = None
        self._goto_error_closes = True
        self._evaluate_error: Exception | None = None
        #: ``None`` unless a test sets it: the common case for a tab this run
        #: attaches to, since attach mode never calls ``set_viewport_size`` (#192).
        self.viewport_size: Mapping[str, int] | None = None
        #: What ``locator(selector).first.bounding_box()`` answers, keyed by
        #: selector. Empty means every selector reads as "no box" -- the
        #: fallback-to-viewport path (#192).
        self.content_boxes: dict[str, Mapping[str, float]] = {}
        #: Several matches for one selector, in document order (``None``: no box, as a
        #: hidden element reads). Wins over ``content_boxes`` for that selector (#439).
        self.match_boxes: dict[str, list[Mapping[str, float] | None]] = {}
        self.locator_calls: list[str] = []
        self.locator_error: Exception | None = None

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls.append(selector)
        return FakeLocator(self, selector)

    @property
    def url(self) -> str:
        return self._url

    def is_closed(self) -> bool:
        return self._closed

    async def goto(self, url: str) -> object:
        assert not self._closed, "navigated a closed tab"
        self.goto_calls.append(url)
        if self._goto_error is not None:
            error, self._goto_error = self._goto_error, None
            if self._goto_error_closes:
                self._closed = True
            raise error
        self._url = url
        return None

    def fail_next_goto(self, error: Exception, *, closes: bool = True) -> None:
        """Arm one failed navigation.

        ``closes`` is the difference the run has to tell apart: a tab that died with
        the navigation, or a page that simply did not load while the tab is fine.
        """
        self._goto_error = error
        self._goto_error_closes = closes

    async def evaluate(self, expression: str) -> Any:
        assert not self._closed, "evaluated on a closed tab"
        self.evaluate_calls.append(expression)
        if self._evaluate_error is not None:
            error, self._evaluate_error = self._evaluate_error, None
            raise error
        return self.context.evaluate_result

    def fail_next_evaluate(self, error: Exception) -> None:
        """Arm one failed in-page evaluate, the way :meth:`fail_next_goto` arms a navigation."""
        self._evaluate_error = error

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True

    def user_closed_it(self) -> None:
        """The tab going away without netkeep noticing: the case ``_ensure_page`` exists for."""
        self._closed = True


def _covers(domain: str, host: str) -> bool:
    domain = domain.lstrip(".")
    return host == domain or host.endswith("." + domain)


def _chrome_covers(domain: str, host: str) -> bool:
    """Chrome's rule: a dotless domain is a host-only cookie, an exact match."""
    if domain.startswith("."):
        return _covers(domain, host)
    return host == domain


def visible_cookies(
    jar: Sequence[Mapping[str, Any]], urls: Sequence[str], *, chrome: bool = False
) -> list[Mapping[str, Any]]:
    """The cookies that would be sent to at least one of ``urls``.

    Domain, path prefix and the secure flag. Two readings differ on the edges:
    Playwright's ``filterCookies`` (``chrome=False``) returns the whole jar for no
    URLs and treats a dotless domain like a dotted one; Chrome's ``Network.getCookies``
    (``chrome=True``) uses the page's own URL for no URLs (nothing, on ``about:blank``)
    and takes a dotless domain as an exact host.
    """
    if not urls:
        if not chrome:
            return list(jar)
        urls = ["about:blank"]
    covers = _chrome_covers if chrome else _covers
    parsed = [urlparse(url) for url in urls]
    return [
        c
        for c in jar
        if any(
            covers(str(c.get("domain", "")), u.hostname or "")
            and (u.path or "/").startswith(str(c.get("path", "/")))
            and (not c.get("secure") or u.scheme == "https")
            for u in parsed
        )
    ]


class CookieSession:
    """A page-level CDP session that answers ``Network.getCookies`` and nothing else.

    Like Chrome, it filters by ``urls`` before replying; an unscoped call (no ``urls``,
    or an empty list) is scoped to the page's own URL, which holds nothing on a blank tab.
    ``calls`` records every ``(method, params)`` sent.
    """

    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.detached = False

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if method != "Network.getCookies":
            raise AssertionError(f"the cookie read sends only Network.getCookies, not {method}")
        context = self._context
        context.cookie_calls += 1
        context.last_cookies_urls = (params or {}).get("urls")
        if context.cookie_error is not None:
            raise context.cookie_error
        if context.cookie_reply is not None:
            return context.cookie_reply
        urls = list((params or {}).get("urls") or ())
        jar = visible_cookies(context.cookie_jar, urls, chrome=True)
        context.last_cookies_returned = [str(c.get("name", "")) for c in jar]
        return {"cookies": jar}

    def on(self, event: str, handler: Any) -> None:
        raise AssertionError("the cookie read listens to nothing")

    async def detach(self) -> None:
        self.detached = True


class FakeContext:
    """The context the user's browsing already lives in. Hands out tabs and a cookie jar."""

    def __init__(
        self,
        *,
        cookies: Sequence[Mapping[str, Any]] = (),
        evaluate_result: Any = None,
        new_page_error: Exception | None = None,
        page_goto_error: Exception | None = None,
    ) -> None:
        #: Armed on every tab this context opens, for the run that loses the tab it
        #: just reopened.
        self.page_goto_error = page_goto_error
        self.pages: list[FakePage] = []
        self.cookie_jar: list[Mapping[str, Any]] = list(cookies)
        self.cookie_error: Exception | None = None
        self.evaluate_result = evaluate_result
        self.new_page_error = new_page_error
        self.new_page_calls = 0
        self.cookie_calls = 0
        self.cookie_sessions: list[CookieSession] = []
        #: A reply ``Network.getCookies`` gives verbatim, for a malformed-answer test.
        self.cookie_reply: Any = None
        #: The ``urls`` argument the most recent ``cookies()`` call passed, for a
        #: test that wants to assert a caller scoped its read (#174 item 7).
        self.last_cookies_urls: str | Sequence[str] | None = None
        #: Names of the cookies the most recent ``cookies()`` call handed back.
        self.last_cookies_returned: list[str] = []

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        if self.new_page_error is not None:
            raise self.new_page_error
        page = FakePage(self)
        if self.page_goto_error is not None:
            page.fail_next_goto(self.page_goto_error)
        self.pages.append(page)
        return page

    async def cookies(self, urls: str | Sequence[str] | None = None) -> Sequence[Mapping[str, Any]]:
        self.cookie_calls += 1
        self.last_cookies_urls = urls
        if self.cookie_error is not None:
            raise self.cookie_error
        # An empty or missing ``urls`` is no filter, as in Playwright: the whole jar.
        wanted = [urls] if isinstance(urls, str) else list(urls or ())
        jar = visible_cookies(self.cookie_jar, wanted)
        self.last_cookies_returned = [str(c.get("name", "")) for c in jar]
        return jar

    async def new_cdp_session(self, page: Any) -> Any:
        session = CookieSession(self)
        self.cookie_sessions.append(session)
        return session

    @property
    def open_pages(self) -> list[FakePage]:
        return [page for page in self.pages if not page.is_closed()]

    async def close(self) -> None:
        raise AssertionError("the context belongs to the user; netkeep never closes it")


class FakeBrowser:
    """The attached Chrome. Creating a context or closing it is a test failure."""

    def __init__(
        self, contexts: Sequence[FakeContext] | None = None, version: str = "Chrome/140.0.7339.80"
    ) -> None:
        self.context_list = [FakeContext()] if contexts is None else list(contexts)
        self.connected = True
        self._version = version

    @property
    def contexts(self) -> Sequence[ContextLike]:
        return self.context_list

    @property
    def version(self) -> str:
        return self._version

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self) -> ContextLike:
        raise AssertionError("a second context is a second LinkedIn device (ADR 0002)")

    async def close(self) -> None:
        raise AssertionError("netkeep neither starts nor stops the user's browser")


class FakeConnector:
    """Stands in for ``connect_over_cdp``. Counts attaches and detaches; cannot launch.

    Pass several browsers to describe a browser that goes away mid-run: the first
    attach gets the first, the reattach gets the next.

    The fake Chrome runs on ``profile``, by default the test's dedicated profile
    (``<data dir>/chrome-profile``), and holds it the way Chrome does: each connect
    writes the profile's ``SingletonLock`` for ``browser_pid``, the browser process the
    connection names (#624). So an ordinary fake attach passes the dedicated-profile
    check by making it. A fake on another profile leaves the dedicated one unheld; a
    ``browser_pid`` of ``None`` is a Chrome that didn't name its process. A sequence of
    pids gives each attach its own, the way ``browsers`` does.
    """

    def __init__(
        self,
        browsers: Sequence[FakeBrowser] | None = None,
        error: Exception | None = None,
        browser_pid: int | Sequence[int | None] | None = FAKE_CHROME_PID,
        profile: Path | None = None,
    ) -> None:
        # Fixed now, as the provider fixes its own when it's built: a test that moves
        # NETKEEP_DATA while a run is live doesn't move the fake Chrome's profile.
        self.profile = data_dir() / CHROME_PROFILE_DIRNAME if profile is None else profile
        self.browsers = [FakeBrowser()] if browsers is None else list(browsers)
        self.error = error
        self.browser_pids = (
            list(browser_pid) if isinstance(browser_pid, Sequence) else [browser_pid]
        )
        self.connect_calls: list[str] = []
        self.detaches = 0

    @property
    def attaches(self) -> int:
        return len(self.connect_calls)

    async def connect(self, cdp_url: str) -> Connection:
        self.connect_calls.append(cdp_url)
        if self.error is not None:
            raise self.error
        browser = self.browsers[min(self.attaches - 1, len(self.browsers) - 1)]
        pid = self.browser_pids[min(self.attaches - 1, len(self.browser_pids) - 1)]
        if pid is not None:
            hold_profile(self.profile, pid)

        async def detach() -> None:
            self.detaches += 1

        return Connection(browser=browser, detach=detach, browser_pid=pid)
