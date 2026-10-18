"""The CSRF guard for the local API (spec section 14.2).

The API binds to loopback with no login, so the threat is a page on some other
site making your browser send a state-changing request to it. Two rules, checked
by :func:`csrf_violation`, stop that: the request must carry
``X-Netkeeper-Client: 1`` (a custom header a cross-site form cannot set), and when
the browser says where the request came from (``Origin``, ``Sec-Fetch-Site``) it
must be this origin. Reads are never blocked by those two.

**DNS rebinding** (#175 review, F8). A page on ``evil.example`` whose name the
attacker re-points at ``127.0.0.1`` reaches this server with ``Host:
evil.example:8000`` and ``Origin: http://evil.example:8000`` -- the same origin
as itself, so the origin rule alone waves it through, header or not (a
same-origin script may set any header). So every request, reads included, must
name this machine in ``Host``: ``127.0.0.1``, ``localhost``, or ``::1``, plus
the configured ``web.host``. Anything else answers ``421 Misdirected Request``.
The port's value is not checked: the attacker's hostname is what gives a
rebinding away, and the Vite dev server proxies ``/api`` with ``changeOrigin:
false``, so a dev request arrives naming the dev server's own port
(``localhost:5173``). A ``Host`` that is not a plain name and an optional port
is refused too (#177): one containing ``@``, ``/``, ``?``, ``#``, a backslash, or whitespace
(``evil.example@127.0.0.1``, ``127.0.0.1/evil``), or with a port that is not a
number from 1 to 65535 (``localhost:abc``). A browser never sends any of these,
so none of them was a way around the check; refusing them keeps the parser from
having to guess what they mean.

With ``web.host = "0.0.0.0"`` (a container published on a LAN), a request to the
machine's LAN address answers ``421`` as well. That is intended: there is no
login, so the server answers only requests addressed to loopback (spec 15).

**The local API key** (#618, ADR 0013). Past the ``Host`` check, every request under
``/api/`` must carry a credential made from the per-install key
(:mod:`netkeeper.services.local_api_key`): the browser's session cookie or a local
client's ``Authorization: Bearer`` key. Without one it answers ``401``. That stops a
program on this machine that only speaks HTTP to whatever answers on a port; the
cross-site rules above still stop a page on another site. :data:`OPEN_API_PATHS` are
the exceptions, each for a reason given there. Paths outside ``/api/`` (the built UI,
``/docs``) hold no data and stay open.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from starlette.requests import cookie_parser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from netkeeper.services.local_api_key import LocalApiKey, cookie_name

CLIENT_HEADER = "x-netkeeper-client"
CLIENT_HEADER_VALUE = "1"
GUARDED_PREFIX = "/api/"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

RULE_CLIENT_HEADER = "client-header"
RULE_ORIGIN = "origin"
RULE_SEC_FETCH_SITE = "sec-fetch-site"
RULE_HOST = "host"
RULE_LOCAL_KEY = "local-key"

#: The ``/api/`` paths that answer without the local API key (#618). Pinned by a test.
OPEN_API_PATHS = frozenset(
    {
        # Liveness for the top bar and the launchd agent: a status and a version.
        "/api/v1/health",
        # Whether this browser is connected, and whether the first-browser claim is open.
        "/api/v1/session",
        # The connect link: its single-use code is the credential.
        "/api/v1/session/connect",
        # The first-browser claim; it refuses once spent, and the CSRF rules still apply.
        "/api/v1/session/claim",
        # Google's redirect back is a cross-site navigation, so a SameSite=Strict cookie
        # never arrives with it; its one-time ``state`` is the credential.
        "/api/v1/mailboxes/oauth/callback",
    }
)

#: The names this server answers to (see the module docstring). Pinned by a test.
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

_MESSAGES = {
    RULE_CLIENT_HEADER: "state-changing requests to the API need the header X-Netkeeper-Client: 1",
    RULE_ORIGIN: "the Origin header does not match this server's origin",
    RULE_SEC_FETCH_SITE: "Sec-Fetch-Site says the request is not same-origin",
    RULE_HOST: (
        "the Host header does not name this machine (127.0.0.1, localhost, or ::1);"
        " netkeeper only answers requests addressed to its loopback address"
    ),
    RULE_LOCAL_KEY: (
        "this browser or client is not connected to netkeeper: open the link"
        " `netkeeper open` prints, or send Authorization: Bearer with the local API key"
    ),
}
#: Characters a ``Host`` header never contains (#177): user info, path, query, fragment.
_NOT_IN_HOST = frozenset("@/?#\\")
_SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def csrf_violation(
    method: str, path: str, headers: Mapping[str, str], *, scheme: str
) -> str | None:
    """Return the name of the rule the request breaks, or None when it may proceed.

    ``headers`` has lower-cased names. ``scheme`` is the request's own scheme; its own
    host and port are read from the ``Host`` header.
    """
    if method.upper() not in STATE_CHANGING_METHODS or not path.startswith(GUARDED_PREFIX):
        return None
    if headers.get(CLIENT_HEADER) != CLIENT_HEADER_VALUE:
        return RULE_CLIENT_HEADER
    site = headers.get("sec-fetch-site")
    origin = headers.get("origin")
    if origin is not None:
        if site in _SAME_ORIGIN_SITES or _same_origin(origin, scheme, headers.get("host", "")):
            return None
        return RULE_ORIGIN
    if site is not None and site not in _SAME_ORIGIN_SITES:
        return RULE_SEC_FETCH_SITE
    return None


def host_violation(headers: Mapping[str, str], allowed: frozenset[str]) -> bool:
    """Whether ``Host`` fails to name one of ``allowed`` (lower-cased hostnames, no port).

    ``Host`` must be a name, or a bracketed IPv6 address, and an optional port
    from 1 to 65535: nothing that reads as user info, a path, a query, or a
    fragment, and no whitespace (#177).
    """
    host = headers.get("host", "")
    if not host or any(char in _NOT_IN_HOST or char.isspace() for char in host):
        return True
    if host.endswith(":"):  # a port separator with no port
        return True
    try:
        parts = urlsplit(f"//{host}")
        hostname = parts.hostname
        port = parts.port  # ValueError when not a number from 0 to 65535
    except ValueError:
        return True
    if port == 0:
        return True
    return hostname is None or hostname.lower() not in allowed


def request_port(headers: Mapping[str, str], scheme: str) -> int:
    """The port the client addressed, from ``Host``, or the scheme's default."""
    try:
        port = urlsplit(f"//{headers.get('host', '')}").port
    except ValueError:
        port = None
    return port if port is not None else _DEFAULT_PORTS.get(scheme.lower(), 80)


def has_local_credential(headers: Mapping[str, str], scheme: str, key: LocalApiKey | None) -> bool:
    """Whether the request carries the session cookie or the Bearer key for ``key``.

    ``headers`` has lower-cased names. No key (a server that has not started) is no.
    """
    if key is None:
        return False
    if key.accepts_bearer(headers.get("authorization")):
        return True
    return has_session_cookie(headers, scheme, key)


def has_session_cookie(headers: Mapping[str, str], scheme: str, key: LocalApiKey) -> bool:
    """Whether the request carries a valid session cookie for ``key`` (not the Bearer key)."""
    # Starlette's lenient parser: other programs on localhost share this host's cookie
    # jar, and one malformed cookie of theirs must not hide ours.
    cookies = cookie_parser(headers.get("cookie", ""))
    return key.accepts_cookie(cookies.get(cookie_name(request_port(headers, scheme))))


def needs_local_credential(path: str) -> bool:
    """Whether a request for ``path`` must carry the local API key's credential."""
    return path.startswith(GUARDED_PREFIX) and path not in OPEN_API_PATHS


def rejection_message(rule: str) -> str:
    """The human-readable reason for a rule name from :func:`csrf_violation`."""
    return _MESSAGES[rule]


def _same_origin(origin: str, scheme: str, host: str) -> bool:
    return _normalize(origin) == _normalize(f"{scheme}://{host}")


def _normalize(url: str) -> tuple[str, str, int | None] | None:
    """``(scheme, host, port)`` with the default port filled in, or None if unparseable."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not hostname:
        return None  # covers "Origin: null" and garbage
    scheme = parts.scheme.lower()
    return scheme, hostname.lower(), port if port is not None else _DEFAULT_PORTS.get(scheme)


def _local_key(scope: Scope) -> LocalApiKey | None:
    """The key the app's lifespan loaded, or None before it has (every guarded path fails)."""
    app = scope.get("app")
    key = getattr(getattr(app, "state", None), "local_api_key", None)
    return key if isinstance(key, LocalApiKey) else None


class CSRFMiddleware:
    """Pure ASGI middleware applying the Host check, the local API key and
    :func:`csrf_violation` to every HTTP request, in that order.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so streaming responses (the SSE
    stream) pass through untouched.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str] = LOOPBACK_HOSTNAMES) -> None:
        self.app = app
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope["headers"]
        }
        if host_violation(headers, self.allowed_hosts):
            response = JSONResponse(
                {"detail": rejection_message(RULE_HOST), "rule": RULE_HOST}, status_code=421
            )
            await response(scope, receive, send)
            return
        scheme = scope.get("scheme", "http")
        if needs_local_credential(scope["path"]) and not has_local_credential(
            headers, scheme, _local_key(scope)
        ):
            response = JSONResponse(
                {"detail": rejection_message(RULE_LOCAL_KEY), "rule": RULE_LOCAL_KEY},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        rule = csrf_violation(scope["method"], scope["path"], headers, scheme=scheme)
        if rule is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": rejection_message(rule), "rule": rule}, status_code=403)
        await response(scope, receive, send)
