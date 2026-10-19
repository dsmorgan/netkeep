"""The last lines of netkeep's own log, filtered and redacted (#655).

The Logs view's read: ``<data>/logs/netkeep.log`` and its rotated files, the newest
``lines`` records that pass a level and a time filter, each as ``at``, ``level``,
``logger`` and ``message``. The same redaction the standard support bundle applies
(:meth:`netkeep.redaction.Redactor.log`) runs on every message before it leaves,
with the live local key and its cookie value added, so a page never shows a marked
personal value, an address or a credential.

A record is whole: a line that does not start with ``date LEVEL logger`` (a known
level, a dotted logger name) continues the record above it (a traceback), and the
redaction keeps only its shape. So does every line while a personal span is open, even
one that starts with a date, as in the support bundle. The redaction runs over the whole
record before it is split into fields, so no field carries text it has not seen.

The log has no UTC offset, so the records of the hour that repeats when daylight saving
time ends cannot be told apart, and the ``since`` and ``until`` filters cannot either.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from netkeep.logging_setup import LOG_DIRNAME, PERSONAL_CLOSE, PERSONAL_OPEN
from netkeep.paths import data_dir
from netkeep.redaction import MESSAGE_LEFT_OUT, SECRET, Redactor
from netkeep.services.support_bundle import log_files

log = logging.getLogger(__name__)

MAX_LINES: Final = 1000
"""The most records one request returns."""
DEFAULT_LINES: Final = 200
SPAN_MAX_LINES: Final = 200
SPAN_MAX_BYTES: Final = 64 * 1024
"""How far a personal span may run before the record is cut (an unterminated marker)."""

LevelName = Literal["debug", "info", "warning", "error", "critical"]
Reason = Literal["no_log_file", "empty_log", "no_match"]

_RANK: Final = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LEVELS: Final = "|".join(_RANK)
_RECORD: Final = re.compile(
    rf"^(\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}}) ({_LEVELS}) ([A-Za-z_][\w.]*)(?: (.*))?$",
    re.DOTALL,
)
_HEADER: Final = re.compile(
    rf"^\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}} (?:{_LEVELS}) [A-Za-z_][\w.]*(?: |$)"
)
_STAMP: Final = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True, slots=True)
class LogLine:
    at: datetime
    """Local time, with its offset."""
    level: str
    logger: str
    message: str


@dataclass(frozen=True, slots=True)
class LogTail:
    lines: tuple[LogLine, ...]
    reason: Reason | None


def _cutoff(moment: datetime | None) -> str | None:
    """``moment`` as the file spells time (local, to the second); naive means local."""
    if moment is None:
        return None
    local = moment.astimezone() if moment.tzinfo is not None else moment
    return local.strftime(_STAMP)


def _span_open(was_open: bool, line: str) -> bool:
    """Whether a personal span is open after ``line``, given whether one was before it."""
    opening, closing = line.rfind(PERSONAL_OPEN), line.rfind(PERSONAL_CLOSE)
    if opening == closing:  # neither marker is here
        return was_open
    return opening > closing


def _records(directory: Path) -> Iterable[str]:
    """Every record of every log file, oldest first, as raw text.

    A span open at the end of a file carries on into the next one: a rotation can fall
    inside a record's span. A span that stays open past :data:`SPAN_MAX_LINES` lines or
    :data:`SPAN_MAX_BYTES` bytes (an unterminated marker) ends its record with
    :data:`~netkeep.redaction.MESSAGE_LEFT_OUT`; the lines after that are dropped
    unread, not split into records, until one holds a closing marker."""
    current: list[str] | None = None
    size = 0
    opened = False  # inside a personal span: the next line continues the record
    dropping = False  # past the cap: skip lines until a closing marker
    for path in log_files(directory):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if dropping:
                        if PERSONAL_CLOSE in line:
                            dropping = line.rfind(PERSONAL_OPEN) > line.rfind(PERSONAL_CLOSE)
                        continue
                    if current is not None and opened:
                        current.append(line)
                        size += len(line)
                    elif _HEADER.match(line):
                        if current is not None:
                            yield "".join(current)
                        current, size = [line], len(line)
                    elif current is not None:
                        current.append(line)
                        size += len(line)
                    else:
                        continue
                    opened = _span_open(opened, line)
                    if opened and (len(current) > SPAN_MAX_LINES or size > SPAN_MAX_BYTES):
                        text = "".join(current)
                        yield text[: text.rfind(PERSONAL_OPEN)] + MESSAGE_LEFT_OUT
                        current, size, opened, dropping = None, 0, False, True
        except OSError as exc:
            log.warning("log tail: cannot read a log file: %s", type(exc).__name__)
            continue
    if current is not None:
        yield "".join(current)


def _scrub_exact(text: str, secrets: Iterable[str]) -> str:
    for value in secrets:
        if value:
            text = text.replace(value, SECRET)
    return text


def recent_lines(
    *,
    lines: int = DEFAULT_LINES,
    level: LevelName | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    secrets: Iterable[str] = (),
    directory: Path | None = None,
) -> LogTail:
    """The newest ``lines`` records (at most :data:`MAX_LINES`) at ``level`` or above, from
    ``since`` through ``until``, oldest first, redacted. ``secrets`` are exact values
    (the local key) to blank wherever they appear."""
    lines = max(1, min(lines, MAX_LINES))
    floor = _RANK[level.upper()] if level is not None else 0
    start, end = _cutoff(since), _cutoff(until)
    directory = data_dir() / LOG_DIRNAME if directory is None else directory
    if not any(True for _ in log_files(directory)):
        return LogTail((), "no_log_file")
    kept: deque[str] = deque(maxlen=lines)
    seen = False
    for record in _records(directory):
        seen = True
        found = _RECORD.match(record.rstrip("\n"))
        if found is None:
            continue
        at, name = found.group(1), found.group(2).upper()
        if _RANK.get(name, 0) < floor:
            continue
        if (start is not None and at < start) or (end is not None and at > end):
            continue
        kept.append(record)
    if not seen:
        return LogTail((), "empty_log")
    redactor = Redactor([(data_dir(), "<data>"), (Path.home(), "~")])
    out: list[LogLine] = []
    secret_values = tuple(secrets)
    for record in kept:
        # Every field comes from the record after the whole redaction pass.
        text = redactor.log(_scrub_exact(record, secret_values))
        found = _RECORD.match(text.rstrip("\n"))
        if found is None:
            continue
        out.append(
            LogLine(
                at=datetime.strptime(found.group(1), _STAMP).astimezone(),
                level=found.group(2),
                logger=found.group(3),
                message=found.group(4) or "",
            )
        )
    return LogTail(tuple(out), None if out else "no_match")
