"""Exception text in a log call is marked personal (#628).

An exception's message can carry a name, a slug, an address or page text, so a log call
that interpolates one wraps it in ``personal_exc`` and the support bundle redacts the
span. A scan of ``netkeeper/`` fails on a new call that does not.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from netkeeper.logging_setup import (
    PERSONAL_CLOSE,
    PERSONAL_OPEN,
    PERSONAL_PATTERN,
    log_file,
    personal_exc,
    setup_logging,
)

PACKAGE = Path(__file__).resolve().parent.parent / "netkeeper"

LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical"})
LOGGER_NAMES = frozenset({"log", "logger", "_log", "_logger", "logging"})
#: Calls whose result carries no message text: the type name, or an already marked value.
SAFE_CALLS = frozenset({"type", "personal", "personal_exc", "_kind"})

#: ``(file relative to netkeeper/, line text)`` pairs allowed to interpolate an exception
#: unmarked, each with the reason. Keep this empty unless the text provably carries
#: nothing personal.
ALLOWED: dict[tuple[str, str], str] = {}


def _bare_uses(arg: ast.expr, name: str) -> list[ast.Name]:
    """Uses of ``name`` in ``arg`` that put its text in the message.

    ``exc.code`` and ``type(exc).__name__`` read one attribute, and ``personal_exc(exc)``
    marks the text, so none of them count.
    """
    safe: set[int] = set()
    for node in ast.walk(arg):
        if isinstance(node, ast.Attribute):
            safe.add(id(node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in SAFE_CALLS
        ):
            safe.update(id(inner) for inner in ast.walk(node))
    return [
        n for n in ast.walk(arg) if isinstance(n, ast.Name) and n.id == name and id(n) not in safe
    ]


def _is_log_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in LOGGER_NAMES
    )


def unmarked_exception_logs(source: str) -> list[int]:
    """Line numbers of log calls that interpolate a caught exception without ``personal_exc``."""
    found: list[int] = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler) or handler.name is None:
            continue
        for node in ast.walk(handler):
            if not _is_log_call(node):
                continue
            assert isinstance(node, ast.Call)
            args = [*node.args, *(k.value for k in node.keywords if k.arg != "exc_info")]
            if any(_bare_uses(arg, handler.name) for arg in args):
                found.append(node.lineno)
    return sorted(set(found))


def test_no_log_call_interpolates_exception_text_unmarked() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        lines = path.read_text().splitlines()
        for lineno in unmarked_exception_logs(path.read_text()):
            if (rel, lines[lineno - 1].strip()) not in ALLOWED:
                offenders.append(f"netkeeper/{rel}:{lineno}")
    assert not offenders, (
        "wrap the exception in personal_exc() (netkeeper.logging_setup), or log "
        f"type(exc).__name__: {offenders}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    pass\nexcept ValueError as exc:\n    log.warning('x: %s', exc)\n",
        "try:\n    pass\nexcept ValueError as e:\n    log.info(f'x: {e}')\n",
        "try:\n    pass\nexcept ValueError as e:\n    logger.debug('x: %s', str(e))\n",
        "try:\n    pass\nexcept ValueError as e:\n    log.error('x: %s', describe(e))\n",
    ],
)
def test_the_scan_flags_unmarked_exception_text(source: str) -> None:
    assert unmarked_exception_logs(source) == [4]


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    pass\nexcept ValueError as e:\n    log.warning('x: %s', personal_exc(e))\n",
        "try:\n    pass\nexcept ValueError as e:\n    log.warning('x: %s', type(e).__name__)\n",
        "try:\n    pass\nexcept OSError as e:\n    log.warning('x: %s', e.errno)\n",
        "try:\n    pass\nexcept ValueError:\n    log.warning('x', exc_info=True)\n",
        "try:\n    pass\nexcept ValueError as e:\n    other.error('x: %s', e)\n",
    ],
)
def test_the_scan_passes_marked_or_type_only_logs(source: str) -> None:
    assert unmarked_exception_logs(source) == []


def test_personal_exc_is_plain_on_stderr_and_marks_only_the_message_in_the_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO")
    exc = ValueError("no such list Quorbelle")
    logging.getLogger("netkeeper.example").warning("failed: %s", personal_exc(exc))
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert capsys.readouterr().err.endswith("failed: no such list Quorbelle\n")
    text = log_file().read_text()
    assert f"failed: ValueError {PERSONAL_OPEN}no such list Quorbelle{PERSONAL_CLOSE}" in text
    redacted = PERSONAL_PATTERN.sub("<personal>", text)
    assert "Quorbelle" not in redacted
    assert redacted.rstrip().endswith("failed: ValueError <personal>")


def test_personal_exc_marks_the_repr_too() -> None:
    setup_logging("INFO")
    logging.getLogger("netkeeper.example").warning(
        "failed: %r", personal_exc(KeyError("Quorbelle"))
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    redacted = PERSONAL_PATTERN.sub("<personal>", log_file().read_text())
    assert "Quorbelle" not in redacted
    assert redacted.rstrip().endswith("failed: KeyError<personal>")
