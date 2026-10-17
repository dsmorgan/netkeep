"""Redaction for the support bundle (#603): personal values out, secrets out.

Two passes, kept apart because they answer to different rules:

* :func:`strip_secrets` runs at **every** level, the full one included. It replaces
  what a credential looks like (an OAuth access or refresh token, a client secret, a
  bearer token, a cookie, a ``token=`` or ``"password":`` pair) with
  :data:`SECRET`. Nothing netkeeper writes should hold one (CLAUDE.md "Secrets"), so
  this is the second line of defense; the first is that the bundle only ever reads
  an allowlist of files and fields.
* :class:`Redactor` runs below the full level. Every value the log file marks
  personal (:data:`netkeeper.logging_setup.PERSONAL_PATTERN`, #602) becomes a stable
  typed placeholder: the same value is the same ``<email-3>`` everywhere in one
  bundle, so a reader can still follow one contact through a run. Then, for what the
  code did not know to mark, regular expressions replace email addresses, phone
  numbers, LinkedIn URLs, slugs and URNs, other web addresses, and the data
  directory and home directory paths (both carry the macOS user name).

A placeholder never reveals the value: it is a type and a counter, numbered in the
order the bundle met the values.
"""

from __future__ import annotations

import bisect
import contextlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from netkeeper.logging_setup import PERSONAL_CLOSE, PERSONAL_OPEN, PERSONAL_PATTERN

SECRET: Final = "<secret>"  # noqa: S105 (the placeholder, not a secret)

# --- secrets: every level ---------------------------------------------------------------

_CREDENTIAL_NAMES: Final = (
    r"access_token|refresh_token|id_token|client_secret|client-secret|api[_-]?key"
    r"|csrf[_-]?token|xsrf[_-]?token|password|passwd|secret|token|authorization"
    r"|li_at|li_rm|li_mc|liap|jsessionid|bcookie|bscookie|lidc|__Secure-[\w-]+|cookie"
)
_CREDENTIAL_TRIGGERS: Final = (
    "token", "secret", "passw", "key", "authorization", "cookie", "csrf", "xsrf",
    "li_at", "li_rm", "li_mc", "liap", "jsessionid", "lidc", "__secure-",
)  # fmt: skip

Replacement = str | Callable[[re.Match[str]], str]


@dataclass(frozen=True, slots=True)
class _Gate:
    """One substitution, run only on the lines that hold one of its ``triggers``
    (lower-case text a match cannot do without). A log runs to megabytes, and a
    pattern tried at every position of every line is what makes redaction slow."""

    pattern: re.Pattern[str]
    triggers: tuple[str, ...]
    replacement: Replacement


_SECRET_GATES: Final[tuple[_Gate, ...]] = (
    # Google: an access token, a refresh token, a client secret.
    _Gate(re.compile(r"ya29\.[\w.-]+"), ("ya29.",), SECRET),
    _Gate(re.compile(r"\b1//[\w.-]{8,}"), ("1//",), SECRET),
    _Gate(re.compile(r"GOCSPX-[\w-]+", re.IGNORECASE), ("gocspx-",), SECRET),
    # Anthropic API keys.
    _Gate(re.compile(r"sk-ant-[\w-]+"), ("sk-ant-",), SECRET),
    _Gate(re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), ("bearer",), f"Bearer {SECRET}"),
    # A header: everything after the colon.
    _Gate(
        re.compile(r"(?i)\b((?:authorization|cookie|set-cookie)\s*:\s*)\S.*"),
        ("authorization", "cookie"),
        rf"\g<1>{SECRET}",
    ),
    # key=value, as a query string, a cookie or a log line spells it.
    _Gate(
        re.compile(rf"(?i)\b({_CREDENTIAL_NAMES})(\s*=\s*)[^\s&;,'\"<>]+"),
        _CREDENTIAL_TRIGGERS,
        rf"\g<1>\g<2>{SECRET}",
    ),
    # An OAuth authorization code in a query string (a credential until it is spent).
    _Gate(re.compile(r"(?i)([?&]code=)[^\s&#'\"<>]+"), ("code=",), rf"\g<1>{SECRET}"),
    # "key": "value" in JSON or a Python repr.
    _Gate(
        re.compile(rf"(?i)([\"']({_CREDENTIAL_NAMES})[\"']\s*:\s*)([\"'])(?:(?!\3).)*\3"),
        _CREDENTIAL_TRIGGERS,
        rf"\g<1>\g<3>{SECRET}\g<3>",
    ),
)


_NEWLINE: Final = re.compile("\n")


def _apply(text: str, gates: Sequence[_Gate]) -> str:
    """``text`` after each gate's substitution, on the lines its triggers pick; a gate
    with no triggers runs last, over all of it.

    The lines are picked in ``text`` as it was given: a substitution only ever writes a
    placeholder, which carries no trigger a later gate needs."""
    lines = text.split("\n")
    lowered = text.lower()
    if lowered.count("\n") != len(lines) - 1:  # a character whose lower case is a newline
        lowered = "\n".join(line.lower() for line in lines)
    starts = [0, *(found.end() for found in _NEWLINE.finditer(lowered))]
    changed = False
    for gate in gates:
        picked: set[int] = set()
        for trigger in gate.triggers:
            at = lowered.find(trigger)
            while at != -1:
                line = bisect.bisect_right(starts, at) - 1
                picked.add(line)
                next_line = starts[line + 1] if line + 1 < len(starts) else len(lowered)
                at = lowered.find(trigger, next_line)
        for line in sorted(picked):
            replaced = gate.pattern.sub(gate.replacement, lines[line])
            if replaced != lines[line]:
                lines[line] = replaced
                changed = True
    text = "\n".join(lines) if changed else text
    for gate in gates:
        if not gate.triggers:  # no line can be ruled out: the whole text at once
            text = gate.pattern.sub(gate.replacement, text)
    return text


def strip_secrets(text: str) -> str:
    """``text`` with everything shaped like a credential replaced by :data:`SECRET`."""
    return _apply(text, _SECRET_GATES)


# --- personal values: below the full level ----------------------------------------------

_EMAIL: Final = re.compile(r"[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URN: Final = re.compile(r"urn:li:[A-Za-z_]+:[^\s,;)\]}\"'<>]+")
_URL: Final = re.compile(r"\bhttps?://[^\s\"'<>)\]}]+", re.IGNORECASE)
_BARE_LINKEDIN: Final = re.compile(
    r"(?<![\w/.-])(?:[\w-]+\.)?linkedin\.com(?:/[^\s\"'<>)\]}]*)?", re.IGNORECASE
)
_SLUG: Final = re.compile(
    r"(?<![\w-])/(in|pub|company|school|checkpoint/[\w-]+)/([^\s/?#\"'<>)\]},;]+)"
)
"""A LinkedIn path's identifying part: a profile's slug, or what follows a checkpoint's
kind (a challenge id), wherever the path appears without its host."""
_PHONE: Final = re.compile(
    r"(?<![\w.:/+-])"
    r"(?:\+\d{1,3}[\s.-]?\d{2,4}[\s.-]?\d{3,4}[\s.-]?\d{3,4}"
    r"|\+\d{8,15}"
    r"|(?:\(\d{2,4}\)\s?|\d{3}[\s.-])\d{3,4}[\s.-]\d{4})"
    r"(?![\w.:/-])"
)
_SLUG_TRIGGERS: Final = ("/in/", "/pub/", "/company/", "/school/", "/checkpoint/")
_HOME_DIRS: Final = re.compile(r"(?<![\w.-])/(Users|home)/(?!<)[^/\s\"'<>]+")
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})  # noqa: S104

_EXCEPTION_LINE: Final = re.compile(r"(?m)^([A-Za-z_][\w.]*): \S.*$")
"""A traceback's last line, ``ValueError: message``: never a record's first line (that
starts with a date), never a frame (those are indented)."""
MESSAGE_LEFT_OUT: Final = "<message left out>"

KINDS: Final = ("personal", "email", "phone", "url", "urn", "slug", "path")
"""The placeholder types, in the order :meth:`Redactor.counts` lists them. ``personal``
is a marked value of no recognizable shape: a name, most often."""


def classify(value: str) -> str:
    """The placeholder type for one marked value."""
    text = value.strip().strip("'\"")
    if _EMAIL.fullmatch(text):
        return "email"
    if text.startswith("urn:li:"):
        return "urn"
    if _URL.match(text) or _BARE_LINKEDIN.match(text):
        return "url"
    if _PHONE.fullmatch(text):
        return "phone"
    if text.startswith(("/", "~", "./", "../")) or re.match(r"^[A-Za-z]:\\", text):
        return "path"
    return "personal"


class Redactor:
    """Replaces personal values with stable typed placeholders, across one bundle.

    ``directories`` are paths whose spelling names the person (the data directory, the
    home directory), each with what to write instead; the longest is tried first, and
    every spelling of each (as given, and resolved) is replaced.
    """

    def __init__(self, directories: Iterable[tuple[Path, str]] = ()) -> None:
        self._seen: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = dict.fromkeys(KINDS, 0)
        spellings: dict[str, str] = {}
        for path, label in directories:
            for spelling in _spellings(path):
                spellings.setdefault(spelling, label)
        self._directories = sorted(spellings.items(), key=lambda item: -len(item[0]))
        self._gates = (
            _Gate(_HOME_DIRS, ("/users/", "/home/"), lambda m: f"/{m.group(1)}/<user>"),
            _Gate(_EMAIL, ("@",), self._typed("email")),
            _Gate(_URN, ("urn:li:",), self._typed("urn")),
            _Gate(_URL, ("http",), self._url),
            _Gate(_BARE_LINKEDIN, ("linkedin.com",), self._typed("url")),
            _Gate(_SLUG, _SLUG_TRIGGERS, self._slug),
            # Every log line has a digit and a separator, so this one runs everywhere.
            _Gate(_PHONE, (), self._typed("phone")),
        )

    def placeholder(self, kind: str, value: str) -> str:
        """The placeholder for ``value``: the same one each time this bundle meets it."""
        key = (kind, value)
        found = self._seen.get(key)
        if found is None:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            found = f"<{kind}-{self._counts[kind]}>"
            self._seen[key] = found
        return found

    def counts(self) -> dict[str, int]:
        """How many distinct values each placeholder type stands for, so far."""
        return {kind: count for kind, count in self._counts.items() if count}

    def text(self, text: str) -> str:
        """``text`` with secrets, marked values, and recognizable personal values replaced."""
        text = strip_secrets(text)
        text = PERSONAL_PATTERN.sub(self._marked, text)
        return self.scrub(text)

    def log(self, text: str) -> str:
        """Log file text: :meth:`text`, after each traceback's exception message is
        left out (the type is kept). A message is whatever the code put in it, a name
        or an address as often as not, and nothing marked it."""
        text = PERSONAL_PATTERN.sub(self._marked, strip_secrets(text))
        return self.scrub(_EXCEPTION_LINE.sub(rf"\g<1>: {MESSAGE_LEFT_OUT}", text))

    def scrub(self, text: str) -> str:
        """The regular-expression pass alone: what nothing marked, but looks personal."""
        for spelling, label in self._directories:
            text = text.replace(spelling, label)
        return _apply(text, self._gates)

    def value(self, value: object) -> object:
        """A JSON-shaped value with every string in it passed through :meth:`text`.

        Keys are strings netkeeper wrote, never a person's, so they are kept."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value

    def _typed(self, kind: str) -> Callable[[re.Match[str]], str]:
        return lambda match: self.placeholder(kind, match.group(0))

    def _slug(self, match: re.Match[str]) -> str:
        return f"/{match.group(1)}/{self.placeholder('slug', match.group(2))}"

    def _marked(self, match: re.Match[str]) -> str:
        inner = match.group(0).removeprefix(PERSONAL_OPEN).removesuffix(PERSONAL_CLOSE)
        if not inner.strip():
            return inner
        return self.placeholder(classify(inner), inner)

    def _url(self, match: re.Match[str]) -> str:
        url = match.group(0)
        try:
            parts = urlsplit(url)
        except ValueError:
            return self.placeholder("url", url)
        if (parts.hostname or "").lower() in _LOOPBACK:
            # netkeeper's own address and the browser's debug port say where things
            # listen; a query string or a fragment can still name somebody.
            kept = f"{parts.scheme}://{parts.netloc}{parts.path}"
            if parts.query:
                kept += f"?{self.placeholder('url', parts.query)}"
            return kept
        return self.placeholder("url", url)


def _spellings(path: Path) -> list[str]:
    """``path`` as given, expanded and resolved, without a trailing slash; never ``/``."""
    found: list[str] = []
    for candidate in (path, path.expanduser()):
        found.append(str(candidate))
        with contextlib.suppress(OSError):
            found.append(str(candidate.resolve()))
    return [spelling.rstrip("/") for spelling in dict.fromkeys(found) if spelling.rstrip("/")]
