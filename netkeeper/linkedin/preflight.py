"""``netkeeper preflight``: is the sidecar ready? Answered without touching the site.

Preflight asks three questions (spec 9.1): can netkeeper attach to the Chrome the
user started, does that profile still hold a LinkedIn session, and does the profile
look like an ordinary Chrome rather than an automated one. The attach itself answers a
fourth: whether that Chrome is netkeeper's dedicated profile at all (#624).

All three are answered locally. The fingerprint comes from a handful of ``navigator``
properties read on the blank tab the run opens. The login state comes from the
browser's own cookie jar, read over the DevTools Protocol. **Preflight navigates
nowhere**: no request reaches linkedin.com, which is what makes it safe to run on a
laptop at any hour and safe to test offline. Whether a session is not merely present
but still accepted is something only a real job can learn, and the run learns it from
the response classification in spec 9.7.

Cookie values are never read, returned, or logged. Preflight reads names, domains,
and expiry times only, at every log level (spec 15, CLAUDE.md).

Nothing here imports the ORM or opens a session (spec 9.10, ADR 0005).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from netkeeper.chrome_profile import ProfileCheck
from netkeeper.linkedin.browser import (
    SINGLE_ACCOUNT_KEY,
    BrowserBusy,
    BrowserProvider,
    BrowserRun,
    BrowserUnavailable,
    PageLike,
    ProfileRefused,
    _CdpSessionLike,
    _TapContext,
)
from netkeeper.logging_setup import personal_exc

log = logging.getLogger(__name__)

#: The cookie Chrome holds a LinkedIn session in, and the one the client reads its
#: CSRF token from. Matched by name against the local jar; neither value is read.
SESSION_COOKIE = "li_at"
CSRF_COOKIE = "JSESSIONID"

#: Cookie-jar domain suffix the session is looked for under. This is a string
#: compared against cookies already in the browser, never a URL anything requests.
COOKIE_DOMAIN_SUFFIX = "linkedin.com"
#: The only URLs the cookie read asks Chrome for (ADR 0012, #633). Chrome returns a
#: cookie when its domain, path and secure flag fit one of these, so it filters before
#: anything leaves the browser: no other site's cookie (Google's, once Gmail is linked)
#: is sent to netkeeper at all. ``www.linkedin.com`` sees cookies set on
#: ``.linkedin.com``, ``.www.linkedin.com`` and ``www.linkedin.com``, which is where both
#: names above live. A cookie on another LinkedIn subdomain is not read; nothing here needs one.
COOKIE_URLS = ("https://www.linkedin.com/",)

# Read on the blank tab the run opens. Nothing here is overridden or spoofed
# anywhere in netkeeper; the point is to show the user what LinkedIn will see.
_FINGERPRINT_JS = """() => ({
  userAgent: navigator.userAgent,
  platform: navigator.platform,
  languages: Array.from(navigator.languages || []),
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  hardwareConcurrency: navigator.hardwareConcurrency,
  webdriver: navigator.webdriver === true,
  pluginCount: navigator.plugins ? navigator.plugins.length : 0,
})"""


class LoginState(StrEnum):
    """What the cookie jar says about the LinkedIn session in this profile.

    ``NO_SESSION`` is deliberately not spelled ``LOGGED_OUT``. P2-03's
    ``classify.Outcome`` has a ``LOGGED_OUT`` of its own, and two ``StrEnum`` members
    that share a value compare and hash equal, so a preflight answer would pass for a
    classified response and could be written as a session flag. The two say different
    things: this one means "no cookie in the jar", that one means "LinkedIn answered
    with a login wall". A test keeps the values of the two enums disjoint.
    """

    LOGGED_IN = "logged_in"
    NO_SESSION = "no_session"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """What the attached profile looks like from inside a page."""

    user_agent: str = ""
    platform: str = ""
    languages: tuple[str, ...] = ()
    timezone: str = ""
    hardware_concurrency: int = 0
    webdriver: bool = False
    plugin_count: int = 0

    @property
    def headless(self) -> bool:
        return "Headless" in self.user_agent

    @property
    def chromium(self) -> bool:
        return "Chrome/" in self.user_agent or "Chromium/" in self.user_agent


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """The answer to "is the sidecar ready", with the reasons.

    ``problems`` stop a run and are what the CLI exits non-zero for; ``warnings`` are
    things worth seeing that do not stop anything. ``profile`` is the attach's
    dedicated-profile check (#624): passed when attached, refused when the attach was
    refused for it, ``None`` when the attach failed before it could be made.
    """

    cdp_url: str
    attached: bool = False
    browser_version: str = ""
    context_count: int = 0
    login: LoginState = LoginState.UNKNOWN
    session_cookies: tuple[str, ...] = ()
    session_expires_at: datetime | None = None
    fingerprint: Fingerprint | None = None
    profile: ProfileCheck | None = None
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when netkeeper could run a job right now."""
        return self.attached and not self.problems


async def preflight(
    provider: BrowserProvider, account: str = SINGLE_ACCOUNT_KEY
) -> PreflightReport:
    """Attach, look around, detach, and report. Never raises for an expected failure.

    Takes the account's activity lock like every other browser path, so a preflight
    during a run reports ``busy`` instead of opening a second connection.
    """
    try:
        async with provider.run(account) as run:
            return await read_session(run, cdp_url=provider.cdp_url)
    except BrowserBusy as exc:
        return PreflightReport(cdp_url=provider.cdp_url, problems=(str(exc),))
    except ProfileRefused as exc:
        return PreflightReport(
            cdp_url=provider.cdp_url, profile=exc.profile_check, problems=(str(exc),)
        )
    except BrowserUnavailable as exc:
        return PreflightReport(cdp_url=provider.cdp_url, problems=(str(exc),))


async def read_session(run: BrowserRun, *, cdp_url: str) -> PreflightReport:
    """Preflight's look around, on a run that is already attached: the fingerprint and
    the cookie jar from the run's own blank tab, and the report.

    :func:`preflight` calls it inside its own attach; the browser worker calls it for a
    Check connection run (#652), whose attach, lock and refusals are the worker's. It
    navigates nowhere, and raises only what the run's own tab raises when Chrome goes away.
    """
    page = await run.ensure_page()
    fingerprint = await _read_fingerprint(page)
    cookies = await _read_session_cookies(run.context, page)
    return _build_report(
        cdp_url=cdp_url,
        browser_version=run.browser.version,
        context_count=len(run.browser.contexts),
        fingerprint=fingerprint,
        cookies=cookies,
        profile=run.profile_check,
    )


async def _read_fingerprint(page: PageLike) -> Fingerprint | None:
    """Read the navigator properties LinkedIn fingerprints on, from the blank tab."""
    try:
        raw = await page.evaluate(_FINGERPRINT_JS)
    except Exception as exc:
        log.warning("could not read the browser fingerprint: %s", personal_exc(exc))
        return None
    if not isinstance(raw, Mapping):
        log.warning("the browser fingerprint came back as %s", type(raw).__name__)
        return None
    values: Mapping[str, Any] = raw
    languages = values.get("languages")
    return Fingerprint(
        user_agent=str(values.get("userAgent", "")),
        platform=str(values.get("platform", "")),
        languages=tuple(str(item) for item in languages) if isinstance(languages, list) else (),
        timezone=str(values.get("timezone", "")),
        hardware_concurrency=_as_int(values.get("hardwareConcurrency")),
        webdriver=bool(values.get("webdriver", False)),
        plugin_count=_as_int(values.get("pluginCount")),
    )


@dataclass(frozen=True, slots=True)
class _SessionCookie:
    """One LinkedIn cookie, by name and expiry. The value is never read."""

    name: str
    expires_at: datetime | None


async def _read_session_cookies(
    context: object, page: PageLike
) -> tuple[_SessionCookie, ...] | None:
    """The LinkedIn cookies in the user's jar, by name. None when the jar is unreadable.

    Sends one CDP ``Network.getCookies`` with ``urls`` set to :data:`COOKIE_URLS`, on a
    session for this run's own blank tab (no new tab), so Chrome filters the jar and
    only LinkedIn's cookies leave the browser (ADR 0012). Playwright's own
    ``context.cookies`` is not used: its Chromium driver fetches the whole profile's jar
    first and filters afterward. ``Network.getCookies`` needs no ``Network.enable``, so
    the session enables nothing and is detached before this returns. A browser that
    refuses the call reads as an unreadable jar (login state unknown), never as logged out.
    It sends nothing to linkedin.com, and this function copies names and expiry times
    only: no code path here can put a cookie value in a log line or a report.
    """
    try:
        session = cast(_CdpSessionLike, await cast(_TapContext, context).new_cdp_session(page))
    except Exception as exc:
        log.warning("could not read the browser's cookie jar: %s", personal_exc(exc))
        return None
    try:
        reply = await session.send("Network.getCookies", {"urls": list(COOKIE_URLS)})
        jar = reply["cookies"]
        if not isinstance(jar, list) or not all(isinstance(item, Mapping) for item in jar):
            raise TypeError(
                f"Network.getCookies answered with {type(jar).__name__}, not a cookie list"
            )
    except Exception as exc:
        log.warning("could not read the browser's cookie jar: %s", personal_exc(exc))
        return None
    finally:
        try:
            await session.detach()
        except Exception as exc:
            log.debug("could not detach the cookie session: %s", personal_exc(exc))
    found: list[_SessionCookie] = []
    for cookie in jar:
        name = str(cookie.get("name", ""))
        domain = str(cookie.get("domain", "")).lstrip(".")
        if name not in (SESSION_COOKIE, CSRF_COOKIE):
            continue
        if domain != COOKIE_DOMAIN_SUFFIX and not domain.endswith(f".{COOKIE_DOMAIN_SUFFIX}"):
            continue
        found.append(_SessionCookie(name=name, expires_at=_expiry(cookie.get("expires"))))
    return tuple(found)


def _build_report(
    *,
    cdp_url: str,
    browser_version: str,
    context_count: int,
    fingerprint: Fingerprint | None,
    cookies: tuple[_SessionCookie, ...] | None,
    profile: ProfileCheck,
) -> PreflightReport:
    """Turn what we saw into a report: state, problems worth stopping for, warnings."""
    problems: list[str] = []
    warnings: list[str] = []
    login, expires_at = _login_state(cookies)
    if login is LoginState.NO_SESSION:
        problems.append(
            "no live LinkedIn session in this Chrome profile: log in to LinkedIn in "
            "the window `netkeeper browser launch` describes"
        )
    elif login is LoginState.UNKNOWN:
        warnings.append("could not read the cookie jar, so the login state is unknown")
    if fingerprint is None:
        warnings.append("could not read the browser fingerprint")
    else:
        warnings.extend(_fingerprint_warnings(fingerprint))
    return PreflightReport(
        cdp_url=cdp_url,
        attached=True,
        browser_version=browser_version,
        context_count=context_count,
        login=login,
        session_cookies=tuple(cookie.name for cookie in cookies or ()),
        session_expires_at=expires_at,
        fingerprint=fingerprint,
        profile=profile,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def _login_state(
    cookies: tuple[_SessionCookie, ...] | None,
) -> tuple[LoginState, datetime | None]:
    """Logged in when the session cookie is present and has not expired."""
    if cookies is None:
        return LoginState.UNKNOWN, None
    session = next((cookie for cookie in cookies if cookie.name == SESSION_COOKIE), None)
    if session is None:
        return LoginState.NO_SESSION, None
    if session.expires_at is not None and session.expires_at <= datetime.now(UTC):
        return LoginState.NO_SESSION, session.expires_at
    return LoginState.LOGGED_IN, session.expires_at


def _fingerprint_warnings(fingerprint: Fingerprint) -> Sequence[str]:
    """Everything about this profile that LinkedIn would find unusual."""
    warnings: list[str] = []
    if fingerprint.headless:
        warnings.append("this Chrome is headless, which LinkedIn can see; use a windowed Chrome")
    if fingerprint.webdriver:
        warnings.append(
            "navigator.webdriver is true: Chrome was started with automation flags, "
            "so start it with only --remote-debugging-port and --user-data-dir"
        )
    if not fingerprint.chromium:
        warnings.append("the attached browser does not report itself as Chrome")
    if not fingerprint.languages:
        warnings.append("the profile reports no navigator.languages")
    if not fingerprint.timezone:
        warnings.append("the profile reports no timezone")
    return warnings


def _expiry(value: Any) -> datetime | None:
    """A cookie's expiry as an aware UTC datetime. Session cookies report -1."""
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)
