"""The processes ``netkeep update`` starts (#685): ``uv tool ...``, and the exec into the
``netkeep`` uv installed.

The one module besides the login item's ``launchctl`` and the Chrome launcher that
imports ``subprocess`` (``tests/test_browser_safety.py`` pins it): one
``subprocess.run`` of ``uv`` with arguments that always begin ``tool``, never a shell.
The exec replaces this process with ``<uv tool bin>/netkeep update --finish`` or
``--rollback``, so no step of an update runs on code it just replaced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

#: What the exec may pass after ``netkeep``: one update step, nothing else.
EXEC_STEPS: Final = frozenset({("update", "--finish"), ("update", "--rollback")})


@dataclass(frozen=True, slots=True)
class UvResult:
    returncode: int
    stdout: str = ""


def find_uv() -> Path | None:
    """The ``uv`` on ``PATH``, else the installer's ``~/.local/bin/uv``."""
    found = shutil.which("uv")
    if found:
        return Path(found)
    local = Path.home() / ".local" / "bin" / "uv"
    return local if local.is_file() and os.access(local, os.X_OK) else None


def run_uv(arguments: Sequence[str], *, capture: bool = False) -> UvResult:
    """``uv <arguments>``: 127 when there is no ``uv``. Only ``uv tool ...`` runs; with
    ``capture`` the output is returned, else it shows as it runs."""
    arguments = list(arguments)
    if arguments[:1] != ["tool"]:
        raise ValueError(f"netkeep runs only uv tool commands, not uv {' '.join(arguments)}")
    uv = find_uv()
    if uv is None:
        return UvResult(127)
    try:
        done = subprocess.run(  # noqa: S603 -- uv, and tool arguments of our own
            [str(uv), *arguments], capture_output=capture, text=True, check=False
        )
    except OSError:
        return UvResult(126)
    return UvResult(done.returncode, done.stdout if capture else "")


def install(arguments: Sequence[str]) -> int:
    """``uv tool install ...``, its output shown; the exit status."""
    return run_uv(arguments).returncode


def tool_bin_dir() -> Path | None:
    """``uv tool dir --bin``: where ``uv tool install`` puts ``netkeep``."""
    done = run_uv(["tool", "dir", "--bin"], capture=True)
    text = done.stdout.strip()
    return Path(text) if done.returncode == 0 and text else None


def exec_netkeep(executable: Path, arguments: Sequence[str]) -> NoReturn:
    """Replace this process with ``executable`` (uv's ``netkeep``) running one update step."""
    if executable.name != "netkeep" or tuple(arguments) not in EXEC_STEPS:
        raise ValueError("netkeep update execs only netkeep update --finish or --rollback")
    for stream in (sys.stdout, sys.stderr):
        stream.flush()
    replace_process = os.execv
    replace_process(executable, [str(executable), *arguments])
    raise RuntimeError("exec returned")
