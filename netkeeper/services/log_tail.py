"""The last lines of netkeeper's own log, filtered and redacted (#655).

The Logs view's read: ``<data>/logs/netkeeper.log`` and its rotated files, the newest
``lines`` records that pass a level and a time filter, each as ``at``, ``level``,
``logger`` and ``message``. The same redaction the standard support bundle applies
(:meth:`netkeeper.redaction.Redactor.log`) runs on every message before it leaves,
with the live local key and its cookie value added, so a page never shows a marked
personal value, an address or a credential.

A record is whole: a line that does not start with a date continues the record above
it (a traceback), and the redaction keeps only its shape.
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

from netkeeper.logging_setup import LOG_DIRNAME
from netkeeper.paths import data_dir
from netkeeper.redaction import RECORD_START, SECRET, Redactor
from netkeeper.services.support_bundle import log_files

log = logging.getLogger(__name__)

MAX_LINES: Final = 1000
"""The most records one request returns."""
DEFAULT_LINES: Final = 200

LevelName = Literal["debug", "info", "warning", "error", "critical"]
Reason = Literal["no_log_file", "empty_log", "no_match"]

_RANK: Final = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_RECORD: Final = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+) (\S+)(?: (.*))?$", re.DOTALL
)
_STAMP: Final = "%Y-%m-%d %H:%M:%S"
_DUMMY_START: Final = "2000-01-01 00:00:00 "
"""Stands in front of a message so the redaction sees it as a record's first line."""


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


def _records(directory: Path) -> Iterable[str]:
    """Every record of every log file, oldest first, as raw text."""
    for path in log_files(directory):
        current: list[str] | None = None
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if RECORD_START.match(line):
                        if current is not None:
                            yield "".join(current)
                        current = [line]
                    elif current is not None:
                        current.append(line)
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
    kept: deque[re.Match[str]] = deque(maxlen=lines)
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
        kept.append(found)
    if not seen:
        return LogTail((), "empty_log")
    redactor = Redactor([(data_dir(), "<data>"), (Path.home(), "~")])
    out: list[LogLine] = []
    secret_values = tuple(secrets)
    for found in kept:
        text = _scrub_exact(found.group(4) or "", secret_values)
        message = redactor.log(_DUMMY_START + text)[len(_DUMMY_START) :]
        logger = _scrub_exact(found.group(3), secret_values)
        out.append(
            LogLine(
                at=datetime.strptime(found.group(1), _STAMP).astimezone(),
                level=found.group(2).upper(),
                logger=redactor.scrub(logger),
                message=message,
            )
        )
    return LogTail(tuple(out), None if out else "no_match")
