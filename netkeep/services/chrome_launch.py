"""What the app holds of the Chrome launcher (#636, ADR 0012): a Protocol and its answers.

The launcher itself is :class:`netkeep.chrome_spawn.ChromeLauncher`, beside
``worker.py`` and outside ``services/``, because it holds netkeep's one process
spawn. ``serve`` puts it on ``app.state.chrome_launcher`` as a :class:`ChromeSpawner`,
the way it puts the run worker there as a :class:`~netkeep.services.runs.RunExecutor`,
so a route can submit a launch to the task runner without importing the module that
spawns. Without a launcher on ``app.state`` (an app that isn't ``serve``'s), the route
answers 503.

#599's **Start Chrome** button (through the route) and ``serve``'s start at startup
both go through :func:`submit_launch`: one task named :data:`LAUNCH_TASK` at a time,
whose result is published as :data:`LAUNCH_EVENT`. So a press while the startup launch
is still settling answers 409, and nothing launches twice. Only setup's first-run button
passes ``first_run=True`` here; ``netkeep setup`` (#684), its equivalent after install,
passes it to ``netkeep open``'s launcher.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from netkeep.services.events import Event

if TYPE_CHECKING:
    from netkeep.services.events import EventBus
    from netkeep.services.tasks import TaskRunner

log = logging.getLogger(__name__)

#: The event a launch submitted from the UI publishes when it's done, for the user who
#: asked: ``{"task_id", "status", "refusal", "message", "pid"}``.
LAUNCH_EVENT: Final = "browser.launch"

#: The task name a launch runs under on the task runner; one at a time.
LAUNCH_TASK: Final = "chrome_launch"


class LaunchStatus(enum.StrEnum):
    """How a launch went."""

    STARTED = "started"
    """netkeep's Chrome wasn't running; it is now, with the UI in an app window."""

    HANDED_OFF = "handed_off"
    """netkeep's Chrome was running; it opened the UI's app window, and the process
    the launcher started exited."""

    SHOWN = "shown"
    """netkeep's Chrome was running with the UI open: the launcher brought that window
    to the front (#678) and started nothing."""

    HANDOFF_UNCONFIRMED = "handoff_unconfirmed"
    """netkeep's Chrome was running and still holds the profile, but the process the
    launcher started exited with a positive status other than 21, so the window can't be
    confirmed. It probably opened: Chrome may exit nonzero after notifying the running
    instance. Status 21 (profile in use) and a signal are :attr:`FAILED`."""

    REFUSED = "refused"
    """A check before the spawn refused; nothing was started. :attr:`LaunchResult.refusal`
    says which."""

    FAILED = "failed"
    """Chrome couldn't be started, or exited with an error."""


class Refusal(enum.StrEnum):
    """Which check refused a launch."""

    NOT_DEDICATED = "not_dedicated"
    """The port answers, but the Chrome on it isn't netkeep's dedicated profile (#624)."""

    PROFILE_WITHOUT_PORT = "profile_without_port"
    """A live process holds the profile's lock, but the port is silent."""

    RUN_HOLDS_BROWSER = "run_holds_browser"
    """A cold start, while a run holds a browser lock."""

    REMOTE_ADDRESS = "remote_address"
    """``linkedin.cdp_url`` names another host, which no Chrome started here can serve."""

    INVALID_PORT = "invalid_port"
    """``linkedin.cdp_url`` names no valid port. The launcher never guesses one."""


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """One launch's answer. ``message`` is for a person; ``pid`` is a started Chrome's."""

    status: LaunchStatus
    message: str
    refusal: Refusal | None = None
    pid: int | None = None

    @property
    def opened(self) -> bool:
        """Whether the UI's window is now open in netkeep's Chrome."""
        return self.status in (LaunchStatus.STARTED, LaunchStatus.HANDED_OFF, LaunchStatus.SHOWN)

    @property
    def reached_chrome(self) -> bool:
        """Whether the launch reached netkeep's Chrome: opened, or a hand-off that
        couldn't be confirmed. ``netkeep open`` exits 0 for these."""
        return self.opened or self.status is LaunchStatus.HANDOFF_UNCONFIRMED


@dataclass(frozen=True, slots=True)
class PortProbe:
    """What the launcher learned about the Chrome on the port, without driving it.

    With the browser lock free, the probe connects once through the attach's connector:
    ``busy`` is false and ``browser_pid`` is the pid the Chrome on the port named for
    itself, or ``None`` when it named none (or didn't answer as Chrome in time).

    With the lock held, a run is attached and a second CDP client would drop its
    connection, so nothing connects: ``busy`` is true and ``verified_pid`` is the Chrome
    pid that run's attach verified as the dedicated profile (#624), from the lock's
    holder note, or ``None`` when the note has none (the run hasn't attached yet).

    ``fronted`` is true only when the launcher asked for a :class:`UiFront`, the lock was
    free, and on that same connection Chrome brought an open UI window to the front
    (#678). Anything else, a failure included, is false, and the launcher hands off.
    """

    busy: bool
    browser_pid: int | None = None
    verified_pid: int | None = None
    fronted: bool = False


@dataclass(frozen=True, slots=True)
class UiFront:
    """The launcher's ask to bring an open UI window to the front, not open another (#678).

    The probe's identity read, after it names the browser's pid and ``dedicated(pid)``
    says that pid is netkeep's Chrome, lists the browser's targets and activates the
    first page whose URL is on ``http://127.0.0.1:<ui_port>`` or
    ``http://localhost:<ui_port>``, on the same connection. ``dedicated`` is the
    launcher's own check (:func:`netkeep.chrome_profile.check_dedicated_profile`).
    """

    ui_port: int
    dedicated: Callable[[int], bool]


class ChromeSpawner(Protocol):
    """What starts netkeep's Chrome. :class:`netkeep.chrome_spawn.ChromeLauncher` is the
    real one. Defined here, on the core side, so the app can hold one without importing
    the module that spawns (``tests/test_browser_safety.py``)."""

    async def start(self, *, first_run: bool = False) -> LaunchResult: ...


class LaunchUnderWay(RuntimeError):
    """A launch is already running on the task runner; :func:`submit_launch` started none."""


def launch_under_way(tasks: TaskRunner) -> bool:
    """Whether a launch task is pending or running."""
    return any(task.name == LAUNCH_TASK for task in tasks.active())


def submit_launch(
    tasks: TaskRunner,
    bus: EventBus,
    launcher: ChromeSpawner,
    *,
    user_id: int,
    first_run: bool = False,
    on_result: Callable[[LaunchResult], None] | None = None,
) -> str:
    """Submit one :meth:`ChromeSpawner.start` to the task runner and return its task id.

    Raises :class:`LaunchUnderWay` while another launch is pending or running. When it
    ends, :data:`LAUNCH_EVENT` is published for ``user_id`` with ``{task_id, status,
    refusal, message, pid}``, a raised launch as ``failed``, and ``on_result`` (``serve``'s
    startup log line) is called with the result. Call it from the event loop.
    """
    if launch_under_way(tasks):
        raise LaunchUnderWay("netkeep's Chrome is already starting")
    task_id = ""

    def publish(status: str, message: str, refusal: str | None, pid: int | None) -> None:
        data = {
            "task_id": task_id,
            "status": status,
            "refusal": refusal,
            "message": message,
            "pid": pid,
        }
        bus.publish(Event(type=LAUNCH_EVENT, data=data, user_id=user_id))

    async def run_launch() -> None:
        try:
            result = await launcher.start(first_run=first_run)
        except Exception as exc:
            # A button waiting on this event must hear an ending either way.
            publish(
                LaunchStatus.FAILED.value, f"the launch failed: {type(exc).__name__}", None, None
            )
            raise
        publish(
            result.status.value,
            result.message,
            None if result.refusal is None else result.refusal.value,
            result.pid,
        )
        if on_result is not None:
            on_result(result)

    task_id = tasks.submit(LAUNCH_TASK, run_launch, user_id=user_id).id
    return task_id
