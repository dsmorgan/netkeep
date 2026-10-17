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

_VALUE: Final = r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s&;,'\"<>]+)"
"""A credential's value: quoted, or up to the next separator."""

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
    # key=value, as a query string, a cookie or a log line spells it; the value bare or
    # quoted (JSESSIONID="ajax:..."), the = written or URL-encoded (%3D).
    _Gate(
        re.compile(rf"(?i)\b({_CREDENTIAL_NAMES})(\s*=\s*|%3D){_VALUE}"),
        _CREDENTIAL_TRIGGERS,
        rf"\g<1>\g<2>{SECRET}",
    ),
    # key: value, unquoted (a header or a log line): csrf-token: ajax:123, token: abc.
    _Gate(
        re.compile(rf"(?i)\b({_CREDENTIAL_NAMES})\b(\s*:\s*)(?!{SECRET}){_VALUE}"),
        _CREDENTIAL_TRIGGERS,
        rf"\g<1>\g<2>{SECRET}",
    ),
    # LinkedIn's CSRF token is the JSESSIONID cookie's value, "ajax:" and digits.
    _Gate(re.compile(r"(?i)\bajax:\d+"), ("ajax:",), SECRET),
    # A browser cookie as a dict ({'name': 'li_at', 'value': '...'}, as Playwright has it).
    _Gate(
        re.compile(
            rf"(?i)([\"']name[\"']\s*:\s*[\"'](?:{_CREDENTIAL_NAMES})[\"'][^}}]*?"
            rf"[\"']value[\"']\s*:\s*)([\"'])(?:(?!\2).)*\2"
        ),
        ("'name'", '"name"'),
        rf"\g<1>\g<2>{SECRET}\g<2>",
    ),
    # The same, with the value before the name.
    _Gate(
        re.compile(
            rf"(?i)([\"']value[\"']\s*:\s*)([\"'])(?:(?!\2).)*\2"
            rf"(?=[^{{}}]*?[\"']name[\"']\s*:\s*[\"'](?:{_CREDENTIAL_NAMES})[\"'])"
        ),
        ("'name'", '"name"'),
        rf"\g<1>\g<2>{SECRET}\g<2>",
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
_URL: Final = re.compile(r"\b(?:https?|wss?|ftp)://[^\s\"'<>)\]}]+", re.IGNORECASE)
_URL_TRIGGERS: Final = ("http", "ws://", "wss://", "ftp://")
_BARE_LINKEDIN: Final = re.compile(
    r"(?<![\w/.-])(?:[\w-]+\.)?linkedin\.com(?:/[^\s\"'<>)\]}]*)?", re.IGNORECASE
)
_SLUG: Final = re.compile(
    r"(?<![\w-])/(in|pub|company|school|checkpoint/[\w-]+)/([^\s/?#\"'<>)\]},;]+)"
)
"""A LinkedIn path's identifying part: a profile's slug, or what follows a checkpoint's
kind (a challenge id), wherever the path appears without its host."""
_SEP: Final = r"[ \t.-]"
"""A separator inside a phone number: never a newline, so a match stays on its line."""
_PHONE: Final = re.compile(
    r"(?<![\w.:/+-])(?:"
    rf"\+\d{{1,3}}{_SEP}?\d{{2,4}}{_SEP}?\d{{3,4}}{_SEP}?\d{{3,4}}"
    r"|\+\d{8,15}"
    rf"|(?:\(\d{{2,4}}\)[ \t]?|\d{{3}}{_SEP})\d{{3,4}}{_SEP}\d{{4}}"
    # The UK: +44 (0)20 7946 0321, 020 7946 0321, 07946 032100.
    rf"|\+\d{{1,3}}[ \t]?\(0\)[ \t]?\d{{2,4}}{_SEP}?\d{{3,4}}{_SEP}?\d{{3,4}}"
    rf"|0\d{{2,4}}{_SEP}\d{{3,4}}{_SEP}\d{{3,4}}"
    r"|0\d{4}[ \t]?\d{6}"
    # France: +33 1 23 45 67 89, 01 23 45 67 89.
    r"|\+\d{1,3}[ \t.]\d(?:[ \t.]\d{2}){4}"
    r"|0\d(?:[ \t.]\d{2}){4}"
    r")(?![\w.:/-])"
)
_LOCAL_PHONE: Final = re.compile(r"(?<![\w.:/+-])\d{3}-\d{4}(?![\w.:/-])")
"""A seven-digit local number, 555-0142: the shape of a range (250-1250) too, so it is
replaced only on a line that says it is a phone number (:data:`_PHONE_WORDS`)."""
_PHONE_WORDS: Final = ("phone", "tel", "call", "mobile", "cell", "sms", "whatsapp")
_SLUG_TRIGGERS: Final = ("/in/", "/pub/", "/company/", "/school/", "/checkpoint/")
_HOME_DIRS: Final = re.compile(r"(?<![\w.-])/(Users|home)/(?!<)[^/\s\"'<>]+")
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})  # noqa: S104

RECORD_START: Final = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
"""The start of a log record: its date and time. Any other line continues the record."""

_PREFIX: Final = re.compile(r"^(\s*(?:[|+]\s*)*)(.*?)\s*$")
_FRAME: Final = re.compile(r'^File ".*", line \d+(?:, in \S.*)?$')
_TRACEBACK_HEADS: Final = frozenset(
    {
        "Traceback (most recent call last):",
        "Exception Group Traceback (most recent call last):",
        "During handling of the above exception, another exception occurred:",
        "The above exception was the direct cause of the following exception:",
    }
)
_RULE: Final = re.compile(r"^\s*\+[-+]*-(?: (?:\d+|\.\.\.) -+)?$")
"""An exception group's rule between sub-exceptions: ``+-+---- 1 ----``, ``+------``."""
_CARETS: Final = re.compile(r"^[~^]+$")
_KEPT_NOTES: Final = re.compile(
    r"^(?:\[Previous line repeated \d+ more times?\]|and \d+ more exceptions?)$"
)
_TYPE_LINE: Final = re.compile(r"^((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)(:.*)?$")
_BUILTIN_TYPE: Final = re.compile(
    r"^[A-Z]\w*(?:Error|Exception|Warning|Exit|Interrupt|Group|Iteration|Cancelled)$"
)
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
    if _PHONE.fullmatch(text) or _LOCAL_PHONE.fullmatch(text):
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
            _Gate(_URL, _URL_TRIGGERS, self._url),
            _Gate(_BARE_LINKEDIN, ("linkedin.com",), self._typed("url")),
            _Gate(_SLUG, _SLUG_TRIGGERS, self._slug),
            # Every log line has a digit and a separator, so this one runs everywhere.
            _Gate(_PHONE, (), self._typed("phone")),
            _Gate(_LOCAL_PHONE, _PHONE_WORDS, self._typed("phone")),
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
        """Log file text: :meth:`text`, with only a record's first line and the shape of a
        traceback kept (:func:`continuations`). A message is whatever the code put in
        it, a name or an address as often as not, and nothing marked it."""
        text = PERSONAL_PATTERN.sub(self._marked, strip_secrets(text))
        return self.scrub(continuations(text))

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
            # listen; a query string or a fragment can still name somebody, and a
            # /devtools/ path names one browser session.
            path = "/devtools/<id>" if parts.path.startswith("/devtools/") else parts.path
            kept = f"{parts.scheme}://{parts.netloc}{path}"
            if parts.query:
                kept += f"?{self.placeholder('url', parts.query)}"
            return kept
        return self.placeholder("url", url)


def strip_url_path(url: str) -> str:
    """``url`` as its scheme, host and port alone (a CDP URL's path names a session)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url


def continuations(text: str) -> str:
    """Log text with every line after a record's first replaced by
    :data:`MESSAGE_LEFT_OUT`, except a traceback's shape.

    Kept: the ``Traceback``, ``During handling`` and ``The above exception`` lines (an
    exception group's too), each frame (``File "...", line N, in f``) with its source
    and caret lines, an exception group's rules, and each exception's type
    (``ValueError``, or a dotted name such as ``sqlalchemy.exc.IntegrityError``)
    without its message. After a type, every line is message until the next head or
    rule, however much it looks like a frame or a type. Everything else, SQLAlchemy's
    ``[parameters: ...]``, pydantic's ``input_value=...``, goes, and a run of left-out
    lines becomes one.
    """
    out: list[str] = []
    in_traceback = in_message = False
    frame_indent: int | None = None  # the last frame's indent, until its source line
    after_source = False  # caret lines may follow a source line
    for line in text.split("\n"):
        if RECORD_START.match(line):
            out.append(line)
            in_traceback = in_message = after_source = False
            frame_indent = None
            continue
        if not line.strip():
            out.append(line)
            continue
        found = _PREFIX.match(line)
        prefix, body = (found.group(1), found.group(2)) if found else ("", line)
        kept: str | None = None
        source = frame_indent
        frame_indent = None
        carets = after_source
        after_source = False
        if body in _TRACEBACK_HEADS:
            in_traceback, in_message, kept = True, False, line
        elif in_traceback and _RULE.match(line):
            in_message, kept = False, line
        elif not in_traceback or in_message:
            pass
        elif _FRAME.match(body):
            frame_indent, kept = len(prefix), line
        elif source is not None and len(prefix) > source:
            after_source, kept = True, line  # the frame's source line: code, not a value
        elif carets and _CARETS.match(body):
            after_source, kept = True, line
        elif _KEPT_NOTES.match(body):
            kept = line
        elif (typed := _TYPE_LINE.match(body)) is not None:
            name = typed.group(1)
            if "." in name or _BUILTIN_TYPE.match(name):
                rest = typed.group(2)
                kept = f"{prefix}{name}" + (
                    f": {MESSAGE_LEFT_OUT}" if rest and rest[1:].strip() else ""
                )
                in_message = True
        if kept is None:
            # An exception group's bar stays, so the drawing still lines up.
            kept = f"{prefix if '|' in prefix else ''}{MESSAGE_LEFT_OUT}"
            if out and out[-1] == kept:
                continue
        out.append(kept)
    return "\n".join(out)


def _spellings(path: Path) -> list[str]:
    """``path`` as given, expanded and resolved, without a trailing slash; never ``/``."""
    found: list[str] = []
    for candidate in (path, path.expanduser()):
        found.append(str(candidate))
        with contextlib.suppress(OSError):
            found.append(str(candidate.resolve()))
    return [spelling.rstrip("/") for spelling in dict.fromkeys(found) if spelling.rstrip("/")]
