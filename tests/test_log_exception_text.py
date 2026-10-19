"""Exception text in a log call is marked personal (#628).

An exception's message can carry a name, a slug, an address or page text, so a log call
that interpolates one wraps it in ``personal_exc`` and the support bundle redacts the
span. A scan of ``netkeep/`` fails on a new call that does not.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from netkeep.logging_setup import (
    PERSONAL_CLOSE,
    PERSONAL_OPEN,
    PERSONAL_PATTERN,
    log_file,
    personal_exc,
    setup_logging,
)

PACKAGE = Path(__file__).resolve().parent.parent / "netkeep"

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
#: Calls whose result carries no message text: the type name, or an already marked value.
SAFE_CALLS = frozenset({"type", "personal", "personal_exc", "_kind"})
#: Attributes of an exception that hold a code or a number, never message text.
SAFE_ATTRS = frozenset({"code", "status", "status_code"})

#: ``(file relative to netkeep/, line text)`` pairs allowed to interpolate an exception
#: unmarked, each with the reason. Keep this empty unless the text provably carries
#: nothing personal.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "linkedin/connections.py",
        'log.warning("connections: %s; stopping incomplete", exc.lost.describe())',
    ): "AnswerLost.lost.describe() is fixed phrases and a number (its docstring says so)",
    (
        "services/inbox_poll.py",
        'log.warning("inbox poll %d: the page answered %s", started_run_id, stop.outcome.value)',
    ): "the value of an outcome enum, never message text",
}


def _is_safe_attribute(node: ast.Attribute) -> bool:
    if node.attr in SAFE_ATTRS:
        return True
    # exc.__class__.__name__
    return (
        node.attr == "__name__"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "__class__"
    )


def _bare_uses(arg: ast.AST, names: set[str]) -> list[ast.Name]:
    """Uses of a name in ``names`` in ``arg`` that put exception text in the message.

    ``type(exc).__name__``, ``exc.code`` and ``personal_exc(exc)`` do not. Any other
    attribute (``exc.args``, ``exc.msg``, ``exc.__cause__``) does.
    """
    safe: set[int] = set()
    for node in ast.walk(arg):
        if (isinstance(node, ast.Attribute) and _is_safe_attribute(node)) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in SAFE_CALLS
        ):
            safe.update(id(inner) for inner in ast.walk(node))
    return [
        n for n in ast.walk(arg) if isinstance(n, ast.Name) and n.id in names and id(n) not in safe
    ]


def _receiver_is_logger(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "logging" or node.id.lower().endswith(("log", "logger"))
    if isinstance(node, ast.Attribute):  # self.log
        return node.attr.lower().endswith(("log", "logger"))
    if isinstance(node, ast.Call):  # logging.getLogger(name).warning(...)
        func = node.func
        return (isinstance(func, ast.Attribute) and func.attr == "getLogger") or (
            isinstance(func, ast.Name) and func.id == "getLogger"
        )
    return False


def _is_log_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and _receiver_is_logger(node.func.value)
    )


def _is_exception_annotation(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    text = ast.unparse(annotation)
    return any(part.endswith(("Exception", "Error")) for part in _words(text))


def _words(text: str) -> list[str]:
    word = ""
    out = []
    for char in text + " ":
        if char.isalnum() or char in "_.":
            word += char
        elif word:
            out.append(word)
            word = ""
    return out


TEXT_PRESERVING_CALLS = frozenset({"str", "repr", "ascii", "format"})
TEXT_PRESERVING_METHODS = frozenset({"format", "strip", "splitlines", "join", "lower"})


def _carries_text(value: ast.expr, names: set[str]) -> bool:
    """Whether ``value`` is exception text: the exception, ``str(exc)``, an f-string of it.

    A call to any other function (``classify(exc)``) is not followed: it returns what its
    author chose, and the scan checks that function on its own.
    """
    if isinstance(value, ast.Call):
        func = value.func
        if (isinstance(func, ast.Name) and func.id in TEXT_PRESERVING_CALLS) or (
            isinstance(func, ast.Attribute) and func.attr in TEXT_PRESERVING_METHODS
        ):
            parts = [*value.args, *(k.value for k in value.keywords)]
            if isinstance(func, ast.Attribute):
                parts.append(func.value)
            return any(_carries_text(part, names) for part in parts)
        return False
    if isinstance(value, ast.Name):
        return value.id in names
    if isinstance(value, ast.Attribute):
        return not _is_safe_attribute(value) and _carries_text(value.value, names)
    return any(
        _carries_text(child, names)
        for child in ast.iter_child_nodes(value)
        if isinstance(child, ast.expr)
    )


def _tainted_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names in ``func`` that hold an exception or text taken from one."""
    names: set[str] = set()
    args = func.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
        if arg is not None and _is_exception_annotation(arg.annotation):
            names.add(arg.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
    changed = True
    while changed:  # err = exc; msg = str(exc)
        changed = False
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if _carries_text(value, names):
                for target in targets:
                    # ``problems[key] = str(exc)`` taints ``problems``, not ``key``.
                    base = target
                    while isinstance(base, (ast.Subscript, ast.Attribute)):
                        base = base.value
                    for name in ast.walk(base):
                        if isinstance(name, ast.Name) and name.id not in names:
                            names.add(name.id)
                            changed = True
    return names


def unmarked_exception_logs(source: str) -> list[int]:
    """Line numbers of log calls that interpolate exception text without ``personal_exc``."""
    found: set[int] = set()
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = _tainted_names(func)
        if not names:
            continue
        for node in ast.walk(func):
            if not _is_log_call(node):
                continue
            assert isinstance(node, ast.Call)
            args = [*node.args, *(k.value for k in node.keywords if k.arg != "exc_info")]
            if any(_bare_uses(arg, names) for arg in args):
                found.add(node.lineno)
    return sorted(found)


def test_no_log_call_interpolates_exception_text_unmarked() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        lines = path.read_text().splitlines()
        for lineno in unmarked_exception_logs(path.read_text()):
            if (rel, lines[lineno - 1].strip()) not in ALLOWED:
                offenders.append(f"netkeep/{rel}:{lineno}")
    assert not offenders, (
        "wrap the exception in personal_exc() (netkeep.logging_setup), or log "
        f"type(exc).__name__: {offenders}"
    )


def _in_handler(statement: str, exc_type: str = "ValueError") -> str:
    return f"def f():\n    try:\n        pass\n    except {exc_type} as e:\n        {statement}\n"


@pytest.mark.parametrize(
    "source",
    [
        _in_handler("log.warning('x: %s', e)"),
        _in_handler("log.info(f'x: {e}')"),
        _in_handler("logger.debug('x: %s', str(e))"),
        _in_handler("log.error('x: %s', describe(e))"),
        _in_handler("log.log(logging.WARNING, 'x: %s', e)"),
        _in_handler("self.log.warning('x: %s', e)"),
        _in_handler("executors_log.warning('x: %s', e)"),
        _in_handler("logging.getLogger('x').warning('x: %s', e)"),
        _in_handler("log.warning('x: %s', e.args)"),
        _in_handler("log.warning('x: %s', e.__cause__)"),
        _in_handler("err = e\n        log.warning('x: %s', err)"),
        _in_handler("msg = str(e)\n        log.warning('x: %s', msg)"),
        "def f(cause: Exception):\n    log.warning('x: %s', cause)\n",
        "def f(cause: BaseException | None):\n    log.warning('x: %s', cause)\n",
        "async def f(cause: OSError):\n    log.warning('x: %s', cause)\n",
    ],
)
def test_the_scan_flags_unmarked_exception_text(source: str) -> None:
    assert unmarked_exception_logs(source) != []


@pytest.mark.parametrize(
    "source",
    [
        _in_handler("log.warning('x: %s', personal_exc(e))"),
        _in_handler("log.warning('x: %s', type(e).__name__)"),
        _in_handler("log.warning('x: %s', e.__class__.__name__)"),
        _in_handler("log.warning('x: %s', e.code)"),
        _in_handler("log.warning('x: %s', e.status_code)"),
        _in_handler("name = type(e).__name__\n        log.warning('x: %s', name)"),
        _in_handler("other.error('x: %s', e)"),
        "def f(count: int):\n    log.warning('x: %s', count)\n",
        "def f():\n    try:\n        pass\n    except ValueError:\n"
        "        log.warning('x', exc_info=True)\n",
    ],
)
def test_the_scan_passes_marked_or_type_only_logs(source: str) -> None:
    assert unmarked_exception_logs(source) == []


def test_personal_exc_is_plain_on_stderr_and_marks_only_the_message_in_the_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO")
    exc = ValueError("no such list Quorbelle")
    logging.getLogger("netkeep.example").warning("failed: %s", personal_exc(exc))
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
    logging.getLogger("netkeep.example").warning("failed: %r", personal_exc(KeyError("Quorbelle")))
    for handler in logging.getLogger().handlers:
        handler.flush()
    redacted = PERSONAL_PATTERN.sub("<personal>", log_file().read_text())
    assert "Quorbelle" not in redacted
    assert redacted.rstrip().endswith("failed: KeyError<personal>")


def test_personal_exc_of_an_empty_message_is_just_the_type() -> None:
    assert str(personal_exc(ValueError()).tagged()) == "ValueError"
    assert repr(personal_exc(ValueError()).tagged()) == "ValueError"
    assert str(personal_exc(ValueError()).tagged()) == str(ValueError.__name__)
    assert str(personal_exc(ValueError())) == ""
