"""What the app holds of the Chrome launcher (#636, ADR 0012): a Protocol and its answers.

The launcher itself is :class:`netkeeper.chrome_spawn.ChromeLauncher`, beside
``worker.py`` and outside ``services/``, because it holds netkeeper's one process
spawn. ``serve`` puts it on ``app.state.chrome_launcher`` as a :class:`ChromeSpawner`,
the way it puts the run worker there as a :class:`~netkeeper.services.runs.RunExecutor`,
so a route can submit a launch to the task runner without importing the module that
spawns. Without a launcher on ``app.state`` (an app that isn't ``serve``'s), the route
answers 503.

#599's **Start Chrome** button and start-at-login, and #549's open-at-login, call
:meth:`ChromeSpawner.start`; only setup's first-run button passes ``first_run=True``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final, Protocol

#: The event a launch submitted from the UI publishes when it's done, for the user who
#: asked: ``{"task_id", "status", "refusal", "message", "pid"}``.
LAUNCH_EVENT: Final = "browser.launch"


class LaunchStatus(enum.StrEnum):
    """How a launch went."""

    STARTED = "started"
    """netkeeper's Chrome wasn't running; it is now, with the UI in an app window."""

    HANDED_OFF = "handed_off"
    """netkeeper's Chrome was running; it opened the UI's app window, and the process
    the launcher started exited."""

    REFUSED = "refused"
    """A check before the spawn refused; nothing was started. :attr:`LaunchResult.refusal`
    says which."""

    FAILED = "failed"
    """Chrome couldn't be started, or exited with an error."""


class Refusal(enum.StrEnum):
    """Which check refused a launch."""

    NOT_DEDICATED = "not_dedicated"
    """The port answers, but the Chrome on it isn't netkeeper's dedicated profile (#624)."""

    PROFILE_WITHOUT_PORT = "profile_without_port"
    """A live process holds the profile's lock, but the port is silent."""

    RUN_HOLDS_BROWSER = "run_holds_browser"
    """A cold start, while a run holds a browser lock."""

    REMOTE_ADDRESS = "remote_address"
    """``linkedin.cdp_url`` names another host, which no Chrome started here can serve."""


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """One launch's answer. ``message`` is for a person; ``pid`` is a started Chrome's."""

    status: LaunchStatus
    message: str
    refusal: Refusal | None = None
    pid: int | None = None

    @property
    def opened(self) -> bool:
        """Whether the UI's window is now open in netkeeper's Chrome."""
        return self.status in (LaunchStatus.STARTED, LaunchStatus.HANDED_OFF)


@dataclass(frozen=True, slots=True)
class PortProbe:
    """What the launcher learned from the Chrome on the port, through the attach's connector.

    ``busy`` is true when a run holds the browser lock, so no second CDP client was
    opened and ``browser_pid`` is unknown. Otherwise ``browser_pid`` is the pid the
    Chrome on the port named for itself, or ``None`` when it named none (or the port
    didn't answer as Chrome).
    """

    busy: bool
    browser_pid: int | None = None


class ChromeSpawner(Protocol):
    """What starts netkeeper's Chrome. :class:`netkeeper.chrome_spawn.ChromeLauncher` is the
    real one. Defined here, on the core side, so the app can hold one without importing
    the module that spawns (``tests/test_browser_safety.py``)."""

    async def start(self, *, first_run: bool = False) -> LaunchResult: ...
