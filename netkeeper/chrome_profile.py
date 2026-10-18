"""Whether the Chrome on the debugging port is netkeeper's dedicated profile (#624).

ADR 0012, rule 6: netkeeper attaches only to the configured port, serving the
dedicated profile (``<data dir>/chrome-profile``). A port alone proves nothing: your
everyday Chrome opted in through ``chrome://inspect``, or a developer's own Chrome,
can answer on it too. So every attach asks one more question before it uses the
browser, and refuses when the answer is anything but "yes".

**How the profile is identified.** Two facts, both read without a side effect:

- the Chrome on the port names its own browser process: ``SystemInfo.getProcessInfo``
  over the attach's own CDP connection lists each process with its type, and the
  one ``"browser"`` entry's ``id`` is that process's pid;
- the profile names the process that holds it: Chrome allows one browser process per
  ``--user-data-dir`` and writes that process into the profile's ``SingletonLock``, a
  symlink whose target is ``<hostname>-<pid>``.

The Chrome on the port is the dedicated profile when the two pids are the same. The
read opens no tab, navigates nothing, reads no cookie, and sends nothing to a site.
``Browser.getBrowserCommandLine`` would name the ``--user-data-dir`` directly, but
Chrome answers it only when it was started with ``--enable-automation``, which
netkeeper's Chrome never is. ``/json/version`` and ``Browser.getVersion`` name no
profile, and ``chrome://version`` needs a tab.

Anything short of a match refuses: another pid (another Chrome), and no answer either
way (no lock, an unreadable lock, a browser that didn't name its process). The check
never falls back to attaching anyway.

This module is plain data and a file read: no CDP, no Playwright, and nothing from
``netkeeper.linkedin``. The attach (:mod:`netkeeper.linkedin.browser`) and the Chrome
launcher (#636), which may not import the extractor, both call
:func:`check_dedicated_profile` with the pid they read.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: The file Chrome writes into a profile directory while a browser process holds it.
SINGLETON_LOCK: Final = "SingletonLock"

#: The process type ``SystemInfo.getProcessInfo`` gives Chrome's own browser process.
BROWSER_PROCESS_TYPE: Final = "browser"


class ProfileVerdict(enum.StrEnum):
    """What the check found. Only :attr:`DEDICATED` lets an attach go ahead."""

    DEDICATED = "dedicated"
    OTHER_PROFILE = "other_profile"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class ProfileCheck:
    """One answer to "is the Chrome on the port netkeeper's dedicated profile?"

    ``reason`` says why, in words a person can act on. ``browser_pid`` is the pid the
    Chrome on the port named, and ``lock_pid`` the one the profile's lock names; either
    is ``None`` when it couldn't be read.
    """

    verdict: ProfileVerdict
    profile: Path
    reason: str
    browser_pid: int | None = None
    lock_pid: int | None = None

    @property
    def dedicated(self) -> bool:
        return self.verdict is ProfileVerdict.DEDICATED

    def refusal(self, cdp_url: str) -> str:
        """The message an attach that this check refused gives."""
        return (
            f"refusing to attach to the Chrome at {cdp_url}: {self.reason}. netkeeper"
            f" attaches only to its own Chrome profile ({self.profile}), so it never drives"
            " a Chrome you started for something else (ADR 0012). Quit that Chrome, then"
            " start netkeeper's with the command `netkeeper browser launch` prints"
        )


def browser_pid_from_process_info(answer: Mapping[str, Any]) -> int | None:
    """The browser process's pid in a ``SystemInfo.getProcessInfo`` answer, or ``None``.

    ``None`` unless there's exactly one ``"browser"`` entry and its ``id`` is a positive
    integer: an answer this can't read is never taken as a match.
    """
    processes = answer.get("processInfo")
    if not isinstance(processes, list):
        return None
    pids = [
        entry.get("id")
        for entry in processes
        if isinstance(entry, Mapping) and entry.get("type") == BROWSER_PROCESS_TYPE
    ]
    if len(pids) != 1:
        return None
    pid = pids[0]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def profile_lock_pid(profile: Path) -> int | None:
    """The pid in ``profile``'s ``SingletonLock``, or ``None`` when there's none to read.

    Chrome's lock is a symlink to ``<hostname>-<pid>``. The hostname is ignored: it
    changes with the network on macOS, and the pid is what the browser on the port is
    compared with.
    """
    try:
        target = str((profile / SINGLETON_LOCK).readlink())
    except OSError:
        return None
    _, dash, tail = target.rpartition("-")
    if not dash or not tail.isdigit():
        return None
    pid = int(tail)
    return pid if pid > 0 else None


def check_dedicated_profile(profile: Path, browser_pid: int | None) -> ProfileCheck:
    """Whether the browser process ``browser_pid`` holds netkeeper's ``profile``.

    ``browser_pid`` is what the Chrome on the port named as its browser process
    (:func:`browser_pid_from_process_info`), or ``None`` when it named none. Reads the
    profile's lock and nothing else.
    """
    lock_pid = profile_lock_pid(profile)
    if browser_pid is None:
        return ProfileCheck(
            ProfileVerdict.UNDETERMINED,
            profile,
            "the Chrome on that port didn't say which process it is, so netkeeper can't"
            " tell which profile it serves",
            lock_pid=lock_pid,
        )
    if lock_pid is None:
        return ProfileCheck(
            ProfileVerdict.UNDETERMINED,
            profile,
            f"no Chrome holds netkeeper's profile (it has no readable {SINGLETON_LOCK}),"
            " so the Chrome on that port can't be shown to be netkeeper's",
            browser_pid=browser_pid,
        )
    if lock_pid != browser_pid:
        return ProfileCheck(
            ProfileVerdict.OTHER_PROFILE,
            profile,
            f"that Chrome (pid {browser_pid}) isn't the one holding netkeeper's profile"
            f" (pid {lock_pid}), so it serves another profile",
            browser_pid=browser_pid,
            lock_pid=lock_pid,
        )
    return ProfileCheck(
        ProfileVerdict.DEDICATED,
        profile,
        f"the Chrome on that port (pid {browser_pid}) holds netkeeper's profile",
        browser_pid=browser_pid,
        lock_pid=lock_pid,
    )
