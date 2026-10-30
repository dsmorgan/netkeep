"""A url origin, parsed so a browser cannot read it a second, different way.

A few places in this package decide whether a url is safe to navigate a real Chrome
to, and each would get it wrong in the same way if it trusted
:func:`urllib.parse.urlsplit` alone: :mod:`netkeep.linkedin.rehearse` refuses
LinkedIn and requires this machine's loopback; :mod:`netkeep.linkedin.page_profiles`
and :mod:`netkeep.linkedin.page_connections` refuse everything but LinkedIn (or, for a
test, loopback). (The in-page fetch this was first written for is gone, #190.) The
first checks were built on ``urlsplit``, and ``urlsplit`` disagrees with a real browser on a
case that matters here.

**The differential.** WHATWG's URL Standard, which every browser implements, treats a
backslash exactly like a forward slash for a "special" scheme (``http``, ``https``
among them). Python's ``urlsplit`` does not. So a url like
``http://www.linkedin.com\\@127.0.0.1:8080`` parses two different ways:

- ``urlsplit`` reads everything before the last ``@`` as userinfo, including the
  backslash, so it reports host ``127.0.0.1`` -- it reads as loopback.
- A browser turns the backslash into a slash first: ``www.linkedin.com`` ends the
  authority, and ``@127.0.0.1:8080`` becomes a path. It navigates to
  ``www.linkedin.com``.

A check written against ``urlsplit``'s answer can be fooled by exactly the url it
exists to catch: it says "loopback", Chrome goes to LinkedIn.

**The fix.** :func:`parse_strict_origin` refuses, before ``urlsplit`` ever sees the
string, everything that could make the two parsers disagree or that has no business
in a bare origin: a backslash anywhere, userinfo (an ``@``), whitespace, a control
character, and anything past the authority -- a path other than a bare trailing
``/``, a query, or a fragment. What survives has one honest reading in either parser,
and the caller then compares the *rebuilt* origin (:meth:`Origin.__str__`, canonical
``scheme://host[:port]``) against a fixed pattern -- never the original string, which
could still carry a spelling ``urlsplit`` and a browser would read alike but that a
human skimming a diff would not notice matches.

**The page readers' guard.** The four ``page_*`` modules each read LinkedIn (or, for a
test or a rehearsal, this machine's loopback) and nothing else. They share
:data:`LINKEDIN_ORIGIN`, :data:`NAVIGATION_LOOPBACK_HOSTS` and
:func:`require_page_origin` from here, so the rule is written once (#698). The
navigation-target set is deliberately not the CDP-host check's set: one says where a
tab may be sent, the other which Chrome netkeep may attach to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote, urljoin, urlsplit

#: LinkedIn's one origin, as :meth:`Origin.__str__` rebuilds it.
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"

#: The loopback hosts a page reader may navigate a tab to instead of LinkedIn: the
#: smoke replica, a rehearsal, a test. Navigation targets only, never a CDP host.
NAVIGATION_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

#: Characters a bare origin never legitimately contains. Every ASCII control
#: character (0x00-0x1F), space (0x20), and DEL (0x7F) -- a browser's own URL
#: parser strips some of these before doing anything else, and reading the
#: string exactly as written, before any such normalization, is the point here.
_CONTROL_OR_SPACE = frozenset(chr(c) for c in range(0x21)) | {chr(0x7F)}

#: A bare origin's path is empty or exactly one trailing slash; anything else
#: (a real path, ``..``, etc.) is not an origin.
_BARE_PATHS = frozenset({"", "/"})


class NotAStrictOrigin(ValueError):
    """A string is not an unambiguous, bare ``scheme://host[:port]`` origin."""


@dataclass(frozen=True, slots=True)
class Origin:
    """A url origin, already validated: scheme, host, and an optional port.

    ``host`` is never bracketed even for an IPv6 literal (matching
    ``urlsplit(...).hostname``); :meth:`__str__` adds the brackets back so the
    rebuilt string is a url a browser can actually parse.
    """

    scheme: str
    host: str
    port: int | None

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port is None:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"


def parse_strict_origin(value: str) -> Origin:
    """``value`` as a bare :class:`Origin`, or raise :class:`NotAStrictOrigin`.

    Every rejection below happens on the raw string, before ``urlsplit`` is asked to
    make sense of it -- see the module docstring for why that order is the point.
    """
    if not value:
        raise NotAStrictOrigin("empty string is not a url")
    if any(ch in _CONTROL_OR_SPACE for ch in value):
        raise NotAStrictOrigin(f"{value!r} contains whitespace or a control character")
    if "\\" in value:
        raise NotAStrictOrigin(
            f"{value!r} contains a backslash, which a browser's URL parser treats as '/'"
        )
    if "@" in value:
        raise NotAStrictOrigin(f"{value!r} carries userinfo ('@'), which a bare origin never does")
    try:
        split = urlsplit(value)
        # ``.hostname`` and ``.port`` are parsed lazily and can each raise on their
        # own (a port that is not a number, for instance) even once ``urlsplit``
        # itself has returned without complaint -- read both inside the same
        # ``try`` so a malformed one is a refusal here, not an exception this
        # function lets escape as something other than :class:`NotAStrictOrigin`.
        hostname = split.hostname
        port = split.port
    except ValueError as exc:
        raise NotAStrictOrigin(f"{value!r} is not a url") from exc
    # Belt and braces: urlsplit itself never reports userinfo once '@' has already
    # been refused above, but a future Python could change that, and this module's
    # whole job is to not trust urlsplit's word alone for the things that matter.
    if split.username is not None or split.password is not None:
        raise NotAStrictOrigin(f"{value!r} carries userinfo, which a bare origin never does")
    if split.path not in _BARE_PATHS or split.query or split.fragment:
        raise NotAStrictOrigin(
            f"{value!r} is not a bare origin: it carries a path, a query, or a fragment"
        )
    if not split.scheme:
        raise NotAStrictOrigin(f"{value!r} has no scheme")
    if not hostname:
        raise NotAStrictOrigin(f"{value!r} has no host")
    return Origin(scheme=split.scheme, host=hostname, port=port)


def require_page_origin(origin: str, *, reader: str) -> str:
    """:data:`LINKEDIN_ORIGIN`, or this machine's loopback, else raise ``ValueError``.

    ``origin`` must be a strict origin (:func:`parse_strict_origin`). LinkedIn's comes
    back as the constant itself; a loopback one (``http`` or ``https``, a host in
    :data:`NAVIGATION_LOOPBACK_HOSTS`) comes back rebuilt. ``reader`` names what reads
    it, for the message: "the inbox", "the prefill".
    """
    try:
        parsed = parse_strict_origin(origin)
    except NotAStrictOrigin as exc:
        raise ValueError(f"{origin!r} is not an origin {reader} may open") from exc
    if str(parsed) == LINKEDIN_ORIGIN:
        return LINKEDIN_ORIGIN
    if parsed.scheme in ("http", "https") and parsed.host in NAVIGATION_LOOPBACK_HOSTS:
        return str(parsed)
    raise ValueError(
        f"{reader} opens {LINKEDIN_ORIGIN!r}, or this machine's own loopback for tests,"
        f" never {origin!r}"
    )


def trim_path(path: str) -> str:
    """``path`` without one trailing ``/``; ``/`` itself stays."""
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def url_path(url: str) -> str:
    """The trimmed path of an absolute ``url``, or of a bare path given as is."""
    return trim_path(urlsplit(url).path if "://" in url else url)


def on_path(url: str, path: str, *, base: str | None = None) -> bool:
    """Whether ``url`` (absolute, or relative to ``base``) is at ``path``, same origin.

    Compared after percent-decoding and case-folding, as LinkedIn's routing reads a
    slug, and after dropping one trailing ``/``. A relative ``url`` takes ``base``'s
    origin; an absolute one must have it. The browser and the page readers share it
    (#719 review).
    """
    try:
        split = urlsplit(urljoin(base, url) if base is not None else url)
        if base is not None:
            want = urlsplit(base)
            if (split.scheme, split.hostname, split.port) != (
                want.scheme,
                want.hostname,
                want.port,
            ):
                return False
    except ValueError:
        return False
    return trim_path(unquote(split.path)).casefold() == trim_path(unquote(path)).casefold()
