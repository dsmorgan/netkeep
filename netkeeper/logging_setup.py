"""Process-wide logging: one line per record, level from ``NETKEEPER_LOG_LEVEL``.

Every record goes to stderr and to :func:`log_file`, ``<data>/logs/netkeeper.log``
(#602), in one format: ``<date> <time> <LEVEL> <logger> <message>``. A line that
does not start with a date continues the record above it (a traceback). The file
rotates by size, keeping a number of old files (``[logs]`` in Settings), and it is
created only when the first record is written. ``NETKEEPER_LOG_STDERR_LEVEL`` raises
the level for stderr alone: the LaunchAgent sets it, so ``serve.err.log`` keeps
warnings and crashes while the rotating file has everything.

**Personal values.** A value the code knows is personal (a name, an email address,
a slug or URN, a URL or file name that can carry one) is logged as
``personal(value)``. The terminal shows it as it is; the file shows it between
:data:`PERSONAL_OPEN` and :data:`PERSONAL_CLOSE`, so a support bundle (#603) can
redact every such span (:data:`PERSONAL_PATTERN`) without guessing. Message text is
never logged at all, personal or not (#517).

The access log never shows the query string of a path in
:data:`REDACTED_QUERY_PATHS`: the Gmail OAuth callback carries the authorization
code there, and a code is a credential until it is spent (CLAUDE.md "Secrets").
Every other query string (a search can name a person) is marked personal.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import sys
from collections.abc import Mapping
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final, TextIO

from netkeeper.paths import data_dir

LEVEL_ENV = "NETKEEPER_LOG_LEVEL"
STDERR_LEVEL_ENV = "NETKEEPER_LOG_STDERR_LEVEL"
HANDLER_NAME = "netkeeper"
FILE_HANDLER_NAME = "netkeeper-file"
DEFAULT_LEVEL = logging.INFO

LOG_DIRNAME: Final = "logs"
LOG_FILENAME: Final = "netkeeper.log"
DEFAULT_MAX_MB: Final = 10
"""Rotate the log file once it would grow past this many megabytes (``logs.max_mb``)."""
DEFAULT_KEEP: Final = 5
"""How many rotated files to keep beside the current one (``logs.keep``)."""
_MB: Final = 1024 * 1024

PERSONAL_OPEN: Final = "\u27e6"  # ⟦
PERSONAL_CLOSE: Final = "\u27e7"  # ⟧
PERSONAL_PATTERN: Final = re.compile(
    f"{PERSONAL_OPEN}[^{PERSONAL_OPEN}{PERSONAL_CLOSE}]*{PERSONAL_CLOSE}"
)
"""One personal value in the log file, markers included."""

#: Paths whose query string the access log replaces with ``?<redacted>``.
REDACTED_QUERY_PATHS = frozenset({"/api/v1/mailboxes/oauth/callback"})
ACCESS_LOGGER = "uvicorn.access"

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

log = logging.getLogger(__name__)


def log_file() -> Path:
    """Where the log file is for the data directory in force; nothing is created."""
    return data_dir() / LOG_DIRNAME / LOG_FILENAME


class _Tagged:
    """A log argument with a plain form (``str``) and a form for the file (:meth:`tagged`)."""

    __slots__ = ()

    def tagged(self) -> object:
        raise NotImplementedError


class Personal(_Tagged):
    """A personal value in a log call: see :func:`personal`."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def tagged(self) -> object:
        return _Marked(self.value)


class _Marked:
    """What a :class:`Personal` becomes in the file: its ``%s`` or ``%r`` form, marked."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def __str__(self) -> str:
        return _mark(str(self.value))

    def __repr__(self) -> str:
        return _mark(repr(self.value))


def personal(value: object) -> Personal:
    """Mark ``value`` personal for one log call: ``log.info("saved %s", personal(name))``.

    It renders as ``str(value)`` (or ``repr`` for ``%r``) on the terminal and between
    markers in the log file. Use it with ``%s`` or ``%r``, never ``%d``.
    """
    return Personal(value)


def _mark(text: str) -> str:
    # A marker inside the value would end the span early, and a redactor would leave the
    # rest; a value never needs one.
    cleaned = text.replace(PERSONAL_OPEN, "?").replace(PERSONAL_CLOSE, "?")
    return f"{PERSONAL_OPEN}{cleaned}{PERSONAL_CLOSE}"


class _QueryPath(_Tagged):
    """An access-log path whose query string is personal: ``/api?⟦q=...⟧`` in the file."""

    __slots__ = ("path", "query")

    def __init__(self, path: str, query: str) -> None:
        self.path = path
        self.query = query

    def __str__(self) -> str:
        return f"{self.path}?{self.query}"

    def tagged(self) -> str:
        return f"{self.path}?{_mark(self.query)}"


class FileFormatter(logging.Formatter):
    """The stderr format, with each :class:`Personal` argument between markers.

    The record is copied before its arguments change, so a handler after this one
    (stderr) still formats the plain values.
    """

    def format(self, record: logging.LogRecord) -> str:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(arg, _Tagged) for arg in args):
            record = copy.copy(record)
            record.args = tuple(_tag(arg) for arg in args)
        elif isinstance(args, Mapping) and any(isinstance(v, _Tagged) for v in args.values()):
            record = copy.copy(record)
            record.args = {k: _tag(v) for k, v in args.items()}
        return super().format(record)


def _tag(arg: object) -> object:
    return arg.tagged() if isinstance(arg, _Tagged) else arg


class RotatingLogFile(RotatingFileHandler):
    """``netkeeper.log``: rotated by size, opened on the first record, private (0600).

    ``serve`` and a command run beside it write the same file. When one of them rotates
    it, the other notices at its next record (the path names another file now) and
    reopens the path rather than writing on into the rotated file. If the file cannot
    be opened at all, one line on stderr says so and the file is left alone from then
    on: logging to the terminal keeps working.
    """

    def __init__(
        self, path: Path, *, max_mb: int = DEFAULT_MAX_MB, keep: int = DEFAULT_KEEP
    ) -> None:
        super().__init__(
            path, maxBytes=max_mb * _MB, backupCount=keep, encoding="utf-8", delay=True
        )
        self.failed = False

    def set_limits(self, *, max_mb: int, keep: int) -> None:
        self.maxBytes = max_mb * _MB
        self.backupCount = keep

    def _open(self) -> TextIOWrapper:
        path = Path(self.baseFilename)
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = super()._open()
        path.chmod(0o600)
        return stream

    def emit(self, record: logging.LogRecord) -> None:
        if self.failed:
            return
        try:
            if self.stream is not None and _moved(self.baseFilename, self.stream):
                self.stream.close()
                self.stream = None
            if self.stream is None:
                self.stream = self._open()
        except OSError as exc:
            self.failed = True
            sys.stderr.write(
                f"netkeeper: cannot write the log file {self.baseFilename}: {exc};"
                " logging to the terminal only\n"
            )
            return
        super().emit(record)


def _moved(path: str, stream: TextIOWrapper) -> bool:
    """Whether ``path`` no longer names the file ``stream`` writes: rotated or removed."""
    try:
        current = Path(path).stat()
    except FileNotFoundError:
        return True
    opened = os.fstat(stream.fileno())
    return (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)


def _file_handler() -> RotatingLogFile | None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, RotatingLogFile) and handler.get_name() == FILE_HANDLER_NAME:
            return handler
    return None


def set_log_rotation(*, max_mb: int, keep: int) -> None:
    """Apply the Settings page's (or config.toml's) ``[logs]`` limits to the log file.

    A value below 1 is ignored (the Settings page refuses one; a file value is the
    operator's typo). Nothing happens before :func:`setup_logging`.
    """
    handler = _file_handler()
    if handler is None:
        return
    if max_mb < 1 or keep < 1:
        log.warning(
            "ignoring log rotation of %d MB, %d files; both must be at least 1", max_mb, keep
        )
        return
    handler.set_limits(max_mb=max_mb, keep=keep)


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Write to whatever ``sys.stderr`` is at emit time.

    Binding the stream lazily (as ``logging.lastResort`` does) means redirection and
    test capture that swap ``sys.stderr`` after setup still see the records.
    """

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        """Ignore the stream the base class assigns; the property always reads sys.stderr."""


class RedactQueryFilter(logging.Filter):
    """Rewrites an access-log record's path when its query string must not be logged.

    uvicorn's access records carry ``(client, method, path, http_version, status)``
    as ``args``; only the path is touched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, query = args[2].partition("?")
            if sep and _comparable(path) in REDACTED_QUERY_PATHS:
                record.args = (*args[:2], f"{path}?<redacted>", *args[3:])
            elif sep:
                record.args = (*args[:2], _QueryPath(path, query), *args[3:])
        return True


def _comparable(path: str) -> str:
    """``path`` without repeated or trailing slashes, lower-cased.

    ``/callback/?code=`` is answered (a redirect to ``/callback?code=``) before the
    route sees it, so each spelling that differs only in slashes or case is redacted.
    """
    return "/" + "/".join(part for part in path.lower().split("/") if part)


def setup_logging(level: str | int | None = None) -> None:
    """Configure the root logger. Safe to call more than once.

    ``level`` is a level name (``DEBUG``, ``info``), a number, or None to read
    ``NETKEEPER_LOG_LEVEL``. An unrecognized value logs a warning and uses INFO.
    ``NETKEEPER_LOG_STDERR_LEVEL``, when set, raises the level stderr alone shows.

    The log file follows the data directory in force: a second call after
    ``NETKEEPER_DATA`` changed closes the old file and writes the new one, keeping the
    rotation limits.
    """
    requested: str | int | None = level if level is not None else os.environ.get(LEVEL_ENV)
    resolved, complaint = _resolve_level(requested)
    stderr_level, stderr_complaint = _resolve_level(
        os.environ.get(STDERR_LEVEL_ENV), STDERR_LEVEL_ENV
    )
    root = logging.getLogger()
    stderr = next((h for h in root.handlers if h.get_name() == HANDLER_NAME), None)
    if stderr is None:
        stderr = _StderrHandler()
        stderr.set_name(HANDLER_NAME)
        stderr.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(stderr)
    stderr.setLevel(
        stderr_level if os.environ.get(STDERR_LEVEL_ENV, "").strip() else logging.NOTSET
    )
    _install_file_handler(root)
    root.setLevel(resolved)
    access = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(item, RedactQueryFilter) for item in access.filters):
        access.addFilter(RedactQueryFilter())
    for problem in (complaint, stderr_complaint):
        if problem is not None:
            log.warning("%s", problem)


def _install_file_handler(root: logging.Logger) -> None:
    path = log_file()
    existing = _file_handler()
    if existing is not None:
        if existing.baseFilename == os.path.abspath(path):  # noqa: PTH100 (as FileHandler spells it)
            return
        limits = {"max_mb": existing.maxBytes // _MB, "keep": existing.backupCount}
        root.removeHandler(existing)
        existing.close()
    else:
        limits = {}
    handler = RotatingLogFile(path, **limits)
    handler.set_name(FILE_HANDLER_NAME)
    handler.setFormatter(FileFormatter(_FORMAT, _DATEFMT))
    root.addHandler(handler)


def remove_file_handler() -> None:
    """Close and detach the log file, if there is one (tests, between data directories)."""
    handler = _file_handler()
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()


def _resolve_level(value: str | int | None, env: str = LEVEL_ENV) -> tuple[int, str | None]:
    """Return the numeric level and, when ``value`` is unusable, a complaint to log."""
    if value is None:
        return DEFAULT_LEVEL, None
    if isinstance(value, int):
        if value >= 0:
            return value, None
        return DEFAULT_LEVEL, f"ignoring invalid log level {value!r}; using INFO"
    text = value.strip()
    if not text:
        return DEFAULT_LEVEL, None
    named = logging.getLevelNamesMapping().get(text.upper())
    if named is not None:
        return named, None
    if text.isdigit():
        return int(text), None
    return DEFAULT_LEVEL, f"ignoring invalid {env} {value!r}; using INFO"
