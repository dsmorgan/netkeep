"""The cookie read asks only for LinkedIn's cookies (#633, ADR 0012).

Once Gmail is linked, the same Chrome profile holds Google's cookies. An unscoped
``context.cookies()`` would copy them into netkeeper's memory, so every ``cookies(``
call in the package passes ``urls``, and the preflight read passes LinkedIn's only.
"""

from __future__ import annotations

import ast
from pathlib import Path

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


def unscoped_calls(source: str) -> list[int]:
    """Line numbers of ``.cookies(...)`` calls that pass no urls, or ``urls=None``."""
    bad: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "cookies"
        ):
            continue
        urls = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "urls":
                urls = keyword.value
        if urls is None or (isinstance(urls, ast.Constant) and urls.value is None):
            bad.append(node.lineno)
    return bad


# --- the fake behaves like the browser ---------------------------------------


async def test_the_fake_returns_google_cookies_to_an_unscoped_read() -> None:
    context = FakeContext(cookies=google_jar())
    names = {item["name"] for item in await context.cookies()}
    assert "SID" in names  # the hazard the scoped read removes


# --- the preflight read ------------------------------------------------------


async def test_the_preflight_read_is_scoped_to_linkedin() -> None:
    from test_preflight import make_context, provider_for

    context = make_context(google_jar())
    await preflight(provider_for(context))
    assert context.last_cookies_urls == COOKIE_URLS
    assert all("linkedin.com" in url for url in COOKIE_URLS)


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
        cookie("JSESSIONID", FAKE_CSRF_VALUE, domain="linkedin.com"),
    ]
    report = await preflight(make_provider(cookies=jar))
    assert report.login is LoginState.LOGGED_IN


async def test_a_google_only_jar_reads_as_no_session() -> None:
    jar = [cookie("SID", GOOGLE_VALUE, domain=".google.com")]
    report = await preflight(make_provider(cookies=jar))
    assert report.login is LoginState.NO_SESSION


# --- every call in the package -----------------------------------------------


def test_every_cookies_call_in_the_package_passes_urls() -> None:
    found = {
        f"{path.relative_to(PACKAGE.parent)}:{n}"
        for path in PACKAGE.rglob("*.py")
        for n in unscoped_calls(path.read_text())
    }
    assert not found, f"pass urls to cookies() (ADR 0012): {sorted(found)}"


def test_the_scanner_flags_an_unscoped_call() -> None:
    assert unscoped_calls("jar = await context.cookies()") == [1]
    assert unscoped_calls("jar = await context.cookies(None)") == [1]
    assert unscoped_calls("jar = await context.cookies(urls=None)") == [1]


def test_the_scanner_passes_a_scoped_call() -> None:
    assert unscoped_calls("jar = await context.cookies(COOKIE_URLS)") == []
    assert unscoped_calls("jar = await context.cookies(urls=['https://x.invalid/'])") == []
