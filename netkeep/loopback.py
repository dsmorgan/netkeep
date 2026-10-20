"""This machine's loopback names, and what a ``linkedin.cdp_url`` names (#701).

One set and one parse for every check that asks "is this address on this machine":
the CDP host checks (posture, the Settings page, the launch instructions, the
launcher), the server's ``Host`` check, the post-install health poll and the Gmail
OAuth redirect. Pure string work, importing nothing from netkeep, so any module may
use it, a request handler included.

A url that does not parse, or names no host, is never loopback: every caller treats
it as not this machine and fails closed. The navigation-target checks (``rehearse``,
the ``page_*`` origin guards) and the redaction set stay separate on purpose; they
answer a different question.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

#: The names that reach this machine's own loopback. Chrome's debug port is only ever
#: on one of them (spec 9.1), and the server answers only requests addressed to one
#: (spec 15).
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})

#: The schemes a ``linkedin.cdp_url`` may use: Chrome's debug endpoint over http, or
#: its websocket directly.
CDP_SCHEMES: Final = frozenset({"http", "https", "ws", "wss"})


def host_of(url: str) -> str | None:
    """The host ``url`` names, lowercased, or ``None`` when it names none or doesn't parse."""
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def is_loopback(url: str) -> bool:
    """Whether ``url`` names one of :data:`LOOPBACK_HOSTS`. Unparseable or hostless: no."""
    return host_of(url) in LOOPBACK_HOSTS


def configured_port(url: str) -> int | None:
    """The port ``url`` names, or ``None`` when it names none or an invalid one.

    Never Chrome's usual 9222 as a guess: netkeep starts, and tells you to start,
    Chrome only on a port you configured.
    """
    try:
        port = urlsplit(url).port
    except ValueError:
        return None
    return port if port is not None and 0 < port < 65536 else None


def cdp_url_problem(cdp_url: str) -> str | None:
    """Why ``cdp_url`` can't be a debug address, or ``None`` when it can (#701).

    The shape only: a scheme, a host and a port, nothing that ``urlsplit`` rejects.
    A host off this machine passes here; posture and the launcher warn about it,
    and only config.toml may set one.
    """
    if any(char.isspace() or not char.isprintable() for char in cdp_url):
        return "has a space, a line break or another unprintable character"
    try:
        parts = urlsplit(cdp_url)
        _ = parts.port  # raises ValueError on a port that isn't a number
    except ValueError:
        return "is not a url"
    if parts.scheme not in CDP_SCHEMES:
        return "must start with http://, https://, ws:// or wss://"
    if not parts.hostname:
        return "names no host"
    if configured_port(cdp_url) is None:
        return "names no port from 1 to 65535"
    return None
