"""The cookie read asks only for LinkedIn's cookies (#633, ADR 0012).

Once Gmail is linked, the same Chrome profile holds Google's cookies. An unscoped
``context.cookies()`` would copy them into netkeeper's memory, so every ``cookies(``
call in the package passes ``urls``, and the preflight read passes LinkedIn's only.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from browser_fakes import FakeContext
from test_preflight import FAKE_CSRF_VALUE, FAKE_SESSION_VALUE, cookie, make_provider

from netkeeper.linkedin.preflight import COOKIE_URLS, LoginState, preflight

PACKAGE = Path(__file__).resolve().parents[1] / "netkeeper"

GOOGLE_VALUE = "fake-google-sid-0000-not-a-session"


def google_jar() -> list[dict[str, object]]:
    return [
        cookie("SID", GOOGLE_VALUE, domain=".google.com"),
        cookie("SAPISID", GOOGLE_VALUE, domain=".accounts.google.com"),
        cookie("li_at", FAKE_SESSION_VALUE, domain=".www.linkedin.com"),
        cookie("JSESSIONID", FAKE_CSRF_VALUE, domain="www.linkedin.com"),
    ]


BANNED_ATTRIBUTES = frozenset({"cookies", "storage_state"})
BANNED_NAMES = frozenset({"cookies", "storage_state"})
BANNED_METHODS = frozenset({"Storage.getCookies", "Network.getAllCookies"})
SCOPED_METHOD = "Network.getCookies"


def _scoped_urls(params: ast.expr | None) -> bool:
    """True when the params are a dict whose ``urls`` is not None or an empty literal."""
    if not isinstance(params, ast.Dict):
        return False
    for key, value in zip(params.keys, params.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "urls":
            if isinstance(value, ast.Constant):
                return False
            return not (isinstance(value, ast.List | ast.Tuple | ast.Set) and not value.elts)
    return False


def jar_reads(source: str) -> list[int]:
    """Line numbers that could read the whole cookie jar.

    Playwright's ``cookies`` and ``storage_state`` (an attribute of anything, aliased
    or called, or named to ``getattr``), the CDP methods that return every cookie, and
    ``Network.getCookies`` sent without a non-empty ``urls``. The one allowed read is
    CDP ``Network.getCookies`` with ``urls``.
    """
    bad: list[int] = []
    allowed: set[int] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == SCOPED_METHOD
            and _scoped_urls(node.args[1] if len(node.args) > 1 else None)
        ):
            allowed.add(id(node.args[0]))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if (
            (isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRIBUTES)
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "hasattr"}
                and any(
                    isinstance(arg, ast.Constant) and arg.value in BANNED_NAMES for arg in node.args
                )
            )
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and (
                    node.value in BANNED_METHODS
                    or (node.value == SCOPED_METHOD and id(node) not in allowed)
                )
            )
        ):
            bad.append(line)
    return bad


# --- the fake behaves like the browser ---------------------------------------


async def test_the_fake_returns_google_cookies_to_an_unscoped_read() -> None:
    context = FakeContext(cookies=google_jar())
    urls: str | list[str] | tuple[str, ...] | None
    for urls in (None, [], ()):
        names = {item["name"] for item in await context.cookies(urls)}
        assert "SID" in names  # the hazard the scoped read removes


async def test_the_fake_honors_path_and_secure_like_the_browser() -> None:
    jar = [
        cookie("li_at", FAKE_SESSION_VALUE),
        {**cookie("elsewhere", "x"), "path": "/foo"},
        {**cookie("insecure-only", "x"), "secure": True},
    ]
    context = FakeContext(cookies=jar)
    assert [c["name"] for c in await context.cookies(["https://www.linkedin.com/"])] == [
        "li_at",
        "insecure-only",
    ]
    assert [c["name"] for c in await context.cookies(["http://www.linkedin.com/"])] == ["li_at"]


# --- the preflight read ------------------------------------------------------


async def test_the_preflight_read_is_scoped_to_linkedin() -> None:
    from test_preflight import make_context, provider_for

    context = make_context(google_jar())
    await preflight(provider_for(context))
    assert context.last_cookies_urls == list(COOKIE_URLS)
    assert all("linkedin.com" in url for url in COOKIE_URLS)


async def test_the_read_is_one_session_that_enables_nothing_and_detaches() -> None:
    from test_preflight import make_context, provider_for

    context = make_context(google_jar())
    await preflight(provider_for(context))
    (session,) = context.cookie_sessions
    assert session.calls == [("Network.getCookies", {"urls": list(COOKIE_URLS)})]
    assert session.detached


async def test_google_cookies_never_reach_the_read() -> None:
    from test_preflight import make_context, provider_for

    context = make_context(google_jar())
    await preflight(provider_for(context))
    assert sorted(context.last_cookies_returned) == ["JSESSIONID", "li_at"]


async def test_session_detection_still_finds_both_cookies() -> None:
    report = await preflight(make_provider(cookies=google_jar()))
    assert report.login is LoginState.LOGGED_IN
    assert report.session_cookies == ("li_at", "JSESSIONID")


async def test_session_detection_accepts_cookies_set_on_the_apex_domain() -> None:
    jar = [
        cookie("li_at", FAKE_SESSION_VALUE, domain=".linkedin.com"),
        cookie("JSESSIONID", FAKE_CSRF_VALUE, domain=".linkedin.com"),
    ]
    report = await preflight(make_provider(cookies=jar))
    assert report.login is LoginState.LOGGED_IN


async def test_a_host_only_apex_cookie_is_not_seen_from_www() -> None:
    """Chrome takes a dotless domain as an exact host, so ``linkedin.com`` is not
    ``www.linkedin.com``'s (the fake matches it; Playwright's looser rule does not)."""
    jar = [cookie("li_at", FAKE_SESSION_VALUE, domain="linkedin.com")]
    report = await preflight(make_provider(cookies=jar))
    assert report.login is LoginState.NO_SESSION


async def test_chrome_scopes_an_empty_urls_to_the_blank_page() -> None:
    context = FakeContext(cookies=google_jar())
    (session,) = [await context.new_cdp_session(None)]
    params: dict[str, Any]
    for params in ({}, {"urls": []}):
        assert (await session.send("Network.getCookies", params))["cookies"] == []
    # Playwright's own read keeps its looser rule: an empty urls is the whole jar.
    assert await context.cookies([])


@pytest.mark.parametrize("reply", [{"cookies": None}, {"cookies": "nope"}, {"cookies": [1]}, {}])
async def test_a_malformed_reply_reads_as_unknown(reply: object) -> None:
    from test_preflight import make_context, provider_for

    context = make_context(google_jar())
    context.cookie_reply = reply
    report = await preflight(provider_for(context))
    assert report.login is LoginState.UNKNOWN


async def test_a_google_only_jar_reads_as_no_session() -> None:
    jar = [cookie("SID", GOOGLE_VALUE, domain=".google.com")]
    report = await preflight(make_provider(cookies=jar))
    assert report.login is LoginState.NO_SESSION


# --- every way to read the jar in the package -------------------------------


def test_nothing_in_the_package_reads_the_whole_jar() -> None:
    found = {
        f"{path.relative_to(PACKAGE.parent)}:{n}"
        for path in PACKAGE.rglob("*.py")
        for n in jar_reads(path.read_text())
    }
    assert not found, (
        f"read cookies only with CDP Network.getCookies and urls (ADR 0012): {sorted(found)}"
    )


def test_the_cookie_urls_are_linkedins_alone() -> None:
    assert COOKIE_URLS
    assert all(urlparse(url).hostname == "www.linkedin.com" for url in COOKIE_URLS)


@pytest.mark.parametrize(
    "snippet",
    [
        "jar = await context.cookies()",
        "jar = await context.cookies(['https://www.linkedin.com/'])",
        "jar = await context.cookies([])",
        "jar = await context.cookies(())",
        "jar = await context.cookies(*a)",
        "f = ctx.cookies\nawait f()",
        "f = getattr(ctx, 'cookies')\nawait f()",
        "await ctx.storage_state()",
        "f = getattr(ctx, 'storage_state')",
        "await session.send('Storage.getCookies')",
        "await session.send('Storage.getCookies', {'browserContextId': b})",
        "await session.send('Network.getAllCookies')",
        "await session.send('Network.getCookies')",
        "await session.send('Network.getCookies', {})",
        "await session.send('Network.getCookies', {'urls': []})",
        "await session.send('Network.getCookies', {'urls': ()})",
        "await session.send('Network.getCookies', {'urls': None})",
        "await session.send('Network.getCookies', params)",
        "method = 'Network.getCookies'\nawait session.send(method, {'urls': u})",
    ],
)
def test_the_scanner_flags_a_read_of_the_whole_jar(snippet: str) -> None:
    assert jar_reads(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "await session.send('Network.getCookies', {'urls': list(COOKIE_URLS)})",
        "await session.send('Network.getCookies', {'urls': ['https://www.linkedin.com/']})",
        "await session.send('Page.getLayoutMetrics')",
    ],
)
def test_the_scanner_passes_the_scoped_read(snippet: str) -> None:
    assert jar_reads(snippet) == []
