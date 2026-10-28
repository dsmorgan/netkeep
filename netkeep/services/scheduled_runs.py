"""The scheduler's job handlers for ``netkeep serve``: a due fire becomes a recorded run (P2-10).

:mod:`netkeep.services.scheduler` decides *when* a kind fires and hands the
fire to a handler; this module is the handler ``netkeep serve`` registers
for each kind with a runner (``SERVED_SCHEDULES``: the two connections syncs
and enrichment). A handler:

1. records nothing while the account's schedule is paused (#324; the
   scheduler's gate already skipped the fire, so this only closes the moment
   between that gate and the handler, and answers
   :attr:`~netkeep.services.scheduler.JobOutcome.PAUSED_AFTER_GATE` so the fire counts as
   skipped, not run), and otherwise records a ``scheduled``
   run through :func:`netkeep.services.runs.create_run`,
   which refuses on a disarmed account -- the scheduler's arm gate already
   skipped the fire, so reaching this refusal means something bypassed that
   gate, and it is logged as an error, nothing runs, and the answer is ``DISARMED_AFTER_GATE``;
2. submits the run to the task runner, so it is a task like any other (its
   progress on the event stream, cancelled with the process), and waits for it,
   because the scheduler's heartbeat runs one fire at a time on purpose;
3. answers :attr:`~netkeep.services.scheduler.JobOutcome.RETRY_LATER` when
   the worker could not reach the browser, and the scheduler parks a retry 20
   to 50 minutes out (spec 9.9);
4. for the inbox poll only (P4-08), answers
   :attr:`~netkeep.services.scheduler.JobOutcome.NOTHING_TO_WATCH` before anything
   else when no live enrollment has a contact with a LinkedIn URN: it records no
   run and attaches to nothing, and the fire counts as skipped;
5. answers :attr:`~netkeep.services.scheduler.JobOutcome.OUTSIDE_HOURS_AFTER_GATE`
   when the worker refused the run outside active hours (#696; the window closed, or
   was narrowed, after the scheduler claimed the fire): the fire counts as skipped, and
   the scheduler moves it to the window's next start;
6. answers :attr:`~netkeep.services.scheduler.JobOutcome.NOT_DONE` when a full
   sync ran but lost some of the page's answers (#200,
   :func:`netkeep.services.runs.lost_answers`): it is incomplete and aged
   nobody, so the week's full sync is not done, and the scheduler offers it again
   a day later instead of a week later.

Once a restart is triggered (#730), the task runner takes no new task. Every handler
then answers :attr:`~netkeep.services.scheduler.JobOutcome.RETRY_LATER` before it records
or claims anything, and logs why; nothing attaches. Should the trigger land after the
record or the claim, the refused run is ended ``aborted`` (``restarting``) and an
auto-send's claim is given back, its step due again, as a run that opened nothing is
(:func:`_refused_at_submit`). The restart gate pauses the scheduler and waits out a
heartbeat in flight before it triggers, so neither should happen; this is the backstop.

The auto-send kind (ADR 0008) has a handler of its own, :func:`_auto_send_handler`.
``serve`` schedules it whatever the flag says, so turning auto-send on on the Settings
page applies from the next fire with no restart (#598). At each fire it first reads
``[campaigns] linkedin_auto_send`` as it is in force for the user then (``config.toml``,
then the Settings page) and, off, claims nothing and notes nothing. On, it
waits out the spacing since the last auto-send, and claims one due ``auto_send`` step
(:func:`netkeep.services.linkedin_steps.claim_auto_send`), which records its scheduled
``message_send`` run; then it runs that run like any other. When it claims nothing it
answers :attr:`~netkeep.services.scheduler.JobOutcome.NOTHING_TO_SEND`.

Nothing here imports the browser: the worker arrives as a
:class:`~netkeep.services.runs.RunExecutor` (``tests/test_browser_safety.py``).
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeep.config import LinkedInSettings, Settings
from netkeep.crm.inbox_apply import has_anything_to_watch, owner_hold
from netkeep.db import off_loop, session_scope
from netkeep.logging_setup import personal_exc
from netkeep.models import Message, SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeep.models.base import utcnow
from netkeep.scoping import scoped
from netkeep.services import runs, ui_settings
from netkeep.services.events import EventBus
from netkeep.services.linkedin_accounts import (
    find_account,
    schedule_paused,
    scheduled_runs_armed,
)
from netkeep.services.linkedin_steps import (
    auto_send_budget_warning,
    auto_send_hold,
    claim_auto_send,
    give_back_unopened,
)
from netkeep.services.scheduler import (
    AUTO_SEND_SPACING_MAX,
    AUTO_SEND_SPACING_MIN,
    SERVED_SCHEDULES,
    ActiveHoursProvider,
    HeartbeatScheduler,
    JobContext,
    JobHandler,
    JobKind,
    JobOutcome,
    JobRegistry,
    KindOffProvider,
    LastFire,
    build_scheduler,
    note_last_fire,
    seed_missing_kinds,
    served_schedules,
    stored_due,
)
from netkeep.services.tasks import SubmitRefused, TaskRunner
from netkeep.services.users import local_users

log = logging.getLogger(__name__)

#: The run kind each scheduled job kind records.
RUN_KIND: Final[dict[JobKind, SyncRunKind]] = {
    JobKind.CONNECTIONS_FULL: SyncRunKind.CONNECTIONS_FULL,
    JobKind.CONNECTIONS_INCREMENTAL: SyncRunKind.CONNECTIONS_INCREMENTAL,
    JobKind.ENRICH: SyncRunKind.ENRICH,
}

#: The inbox poll's run kind. Its handler is registered with the others (P4-01, #380).
INBOX_RUN_KIND: Final = {JobKind.INBOX: SyncRunKind.INBOX}

#: The task name a run is submitted under.
RUN_TASK_NAME: Final = "linkedin.run"


def serve_registry(
    factory: sessionmaker[Session],
    executor: runs.RunExecutor,
    tasks: TaskRunner,
    *,
    clock: Callable[[], datetime],
    settings: Settings | None = None,
) -> JobRegistry:
    """One handler per kind in :data:`RUN_KIND` and :data:`INBOX_RUN_KIND`, each recording
    and running a scheduled run. The inbox handler first checks there is anyone to watch.
    The auto-send kind's handler is always registered: it reads the flag for the user at
    each fire, over ``settings`` (the file's; the defaults when None), #598."""
    registry: dict[JobKind, JobHandler] = {
        kind: _handler(factory, executor, tasks, run_kind, clock=clock)
        for kind, run_kind in RUN_KIND.items()
    }
    for kind, run_kind in INBOX_RUN_KIND.items():
        registry[kind] = _handler(factory, executor, tasks, run_kind, clock=clock, watch_check=True)
    registry[JobKind.AUTO_SEND] = _auto_send_handler(
        factory, executor, tasks, settings if settings is not None else Settings(), clock=clock
    )
    return registry


def submit_run(
    tasks: TaskRunner, executor: runs.RunExecutor, run_id: int, user_id: int
) -> tuple[str, Callable[[], runs.RunOutcome]]:
    """Submit run ``run_id`` to the task runner. Returns the task id and a reader
    for the outcome once the task has finished (``DONE`` until then)."""
    outcome: list[runs.RunOutcome] = []

    async def work() -> None:
        outcome.append(await executor.execute(run_id, user_id))

    info = tasks.submit(RUN_TASK_NAME, work, user_id=user_id)
    return info.id, lambda: outcome[0] if outcome else runs.RunOutcome.DONE


#: The stop reason of a scheduled run the task runner refused, a restart triggered (#730).
REFUSED_RESTARTING: Final = "restarting"


def _restarting(tasks: TaskRunner, what: str) -> bool:
    """Whether the task runner refuses new tasks (#730: a restart was triggered). Logs the
    fire it stops; the handler then records and attaches to nothing."""
    if tasks.refusing is None:
        return False
    log.info("scheduled %s not started: %s; it is tried again later", what, tasks.refusing)
    return True


def _refused_at_submit(
    factory: sessionmaker[Session], user_id: int, run_id: int, reason: str, *, now: datetime
) -> None:
    """End run ``run_id``, recorded but refused by the task runner (#730), ``aborted``:
    it never started, so nothing was opened or typed. An auto-send's claimed message is
    given back, its step due again, as :mod:`netkeep.services.message_send` gives back a
    run that opened nothing."""
    with session_scope(factory, write=True) as session:
        user = session.get(User, user_id)
        if user is None:
            return
        claimed = session.scalars(
            scoped(user, Message).where(Message.sync_run_id == run_id)
        ).first()
        if claimed is not None:
            give_back_unopened(session, user, claimed.id, now=now)
        runs.finish_run(
            session,
            user,
            run_id,
            status=SyncRunStatus.ABORTED,
            now=now,
            stop_reason=REFUSED_RESTARTING,
            error=reason,
        )


async def _submit_or_refuse(
    factory: sessionmaker[Session],
    executor: runs.RunExecutor,
    tasks: TaskRunner,
    run_id: int,
    user_id: int,
    *,
    what: str,
    clock: Callable[[], datetime],
) -> tuple[str, Callable[[], runs.RunOutcome]] | None:
    """:func:`submit_run`, or None when the task runner refused it (#730): the run is
    ended by :func:`_refused_at_submit` and the refusal logged; nothing attaches."""
    try:
        return submit_run(tasks, executor, run_id, user_id)
    except SubmitRefused as exc:
        log.warning("scheduled %s run %d not started: %s", what, run_id, personal_exc(exc))
        try:
            await off_loop(_refused_at_submit, factory, user_id, run_id, str(exc), now=clock())
        except Exception:
            # The next start fails a run left running (fail_interrupted_runs).
            log.exception("scheduled %s: could not end the refused run %d", what, run_id)
        return None


class _SchedulePaused(Exception):
    """The schedule was paused between the scheduler's gate and the handler (#324)."""


def _handler(
    factory: sessionmaker[Session],
    executor: runs.RunExecutor,
    tasks: TaskRunner,
    run_kind: SyncRunKind,
    *,
    clock: Callable[[], datetime],
    watch_check: bool = False,
) -> JobHandler:
    def nothing_to_watch(ctx: JobContext) -> bool:
        with session_scope(factory) as session:
            user = session.get(User, ctx.user_id)
            return user is None or not has_anything_to_watch(session, user)

    def first_poll_is_unsupervised(ctx: JobContext) -> bool:
        with session_scope(factory) as session:
            user = session.get(User, ctx.user_id)
            return user is not None and not runs.has_completed_inbox_poll(session, user)

    def owner_is_unsupervised(ctx: JobContext) -> bool:
        with session_scope(factory) as session:
            user = session.get(User, ctx.user_id)
            return user is not None and owner_hold(session, user)

    def record(ctx: JobContext) -> int | None:
        with session_scope(factory, write=True) as session:
            user = session.get(User, ctx.user_id)
            if user is None:
                log.error("scheduled %s: no user %d", run_kind.value, ctx.user_id)
                return None
            if schedule_paused(session, user, ctx.account_id):
                # Paused between the scheduler's gate and here (#324): nothing new starts.
                raise _SchedulePaused
            return runs.create_run(
                session, user, run_kind, trigger=SyncRunTrigger.SCHEDULED, now=clock()
            ).id

    async def handle(ctx: JobContext) -> JobOutcome | None:
        if _restarting(tasks, run_kind.value):
            return JobOutcome.RETRY_LATER
        if watch_check and await off_loop(nothing_to_watch, ctx):
            log.info(
                "scheduled %s not started: no live enrollment has a LinkedIn contact",
                run_kind.value,
            )
            return JobOutcome.NOTHING_TO_WATCH
        if watch_check and await off_loop(first_poll_is_unsupervised, ctx):
            log.info(
                "scheduled %s not started: no inbox poll has completed, so the first is"
                " run by hand (`netkeep linkedin inbox`)",
                run_kind.value,
            )
            return JobOutcome.FIRST_POLL_BY_HAND
        if watch_check and await off_loop(owner_is_unsupervised, ctx):
            log.info(
                "scheduled %s not started: the inbox owner was forgotten, so the poll"
                " that records it again is run by hand (`netkeep linkedin inbox`)",
                run_kind.value,
            )
            return JobOutcome.OWNER_BY_HAND
        try:
            # Off the event loop, in one writer session as before (#259).
            run_id = await off_loop(record, ctx)
            if run_id is None:
                return None
        except _SchedulePaused:
            log.info("scheduled %s not started: the schedule is paused", run_kind.value)
            return JobOutcome.PAUSED_AFTER_GATE
        except runs.ScheduledRunsDisarmed:
            log.error(
                "scheduled %s reached a disarmed account %d past the scheduler's arm gate;"
                " nothing ran",
                run_kind.value,
                ctx.account_id,
            )
            return JobOutcome.DISARMED_AFTER_GATE
        except runs.RunAlreadyRunning as exc:
            log.info("scheduled %s not started: %s", run_kind.value, personal_exc(exc))
            return JobOutcome.RETRY_LATER
        submitted = await _submit_or_refuse(
            factory, executor, tasks, run_id, ctx.user_id, what=run_kind.value, clock=clock
        )
        if submitted is None:
            return JobOutcome.RETRY_LATER
        task_id, result = submitted
        await tasks.wait(task_id)
        if result() is runs.RunOutcome.RETRY_LATER:
            return JobOutcome.RETRY_LATER
        if await off_loop(_refused_outside_hours, factory, ctx.user_id, run_id):
            log.info(
                "scheduled %s run %d refused outside active hours; it did not run",
                run_kind.value,
                run_id,
            )
            return JobOutcome.OUTSIDE_HOURS_AFTER_GATE
        lost = (
            await off_loop(_lost_answers, factory, ctx.user_id, run_id)
            if run_kind is SyncRunKind.CONNECTIONS_FULL
            else 0
        )
        if lost:
            log.info("scheduled full sync run %d lost answers; it is not done", run_id)
            return JobOutcome.NOT_DONE
        return None

    return handle


def auto_send_spacing_left(session: Session, user: User, now: datetime) -> timedelta:
    """How long until the next auto-send may start (ADR 0008): the spacing after the
    newest ``message_send`` run, an auto-send or a prefill a person started, drawn once
    per run between
    :data:`~netkeep.services.scheduler.AUTO_SEND_SPACING_MIN` and
    :data:`~netkeep.services.scheduler.AUTO_SEND_SPACING_MAX` minutes, less the time
    since it started. Zero when there is none, or the spacing has passed. Read-only."""
    spacing = auto_send_spacing(session, user)
    if spacing is None:
        return timedelta(0)
    return max(spacing.ends_at - now, timedelta(0))


@dataclass(frozen=True, slots=True)
class Spacing:
    """The spacing after the newest ``message_send`` run: when that run started, and when
    the spacing drawn for it ends. It always ends between :data:`AUTO_SEND_SPACING_MIN`
    and :data:`AUTO_SEND_SPACING_MAX` minutes after ``started_at``."""

    run_id: int
    started_at: datetime
    ends_at: datetime

    @property
    def earliest(self) -> datetime:
        return self.started_at + timedelta(minutes=AUTO_SEND_SPACING_MIN)

    @property
    def latest(self) -> datetime:
        return self.started_at + timedelta(minutes=AUTO_SEND_SPACING_MAX)


def auto_send_spacing(session: Session, user: User) -> Spacing | None:
    """The spacing :func:`auto_send_spacing_left` counts down, or ``None`` when no
    ``message_send`` run exists. Read-only."""
    last = session.scalars(
        scoped(user, SyncRun)
        .where(SyncRun.kind == SyncRunKind.MESSAGE_SEND)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).first()
    if last is None:
        return None
    # Seeded by the run's id, so one run's spacing is the same at every heartbeat.
    minutes = random.Random(last.id).uniform(AUTO_SEND_SPACING_MIN, AUTO_SEND_SPACING_MAX)  # noqa: S311 -- pacing
    return Spacing(last.id, last.started_at, last.started_at + timedelta(minutes=minutes))


#: The auto-send handler's words for a fire with ``[campaigns] linkedin_auto_send`` off.
AUTO_SEND_OFF_WORDS: Final = "auto-send is off"


class FireStop(enum.Enum):
    """Why the auto-send handler stops before its claim (:func:`auto_send_fire_check`)."""

    PAUSED = "paused"
    DISARMED = "disarmed"
    HELD = "held"
    SPACING = "spacing"


@dataclass(frozen=True, slots=True)
class FireCheck:
    """What :func:`auto_send_fire_check` found: the stop, or none and the time it read."""

    stop: FireStop | None
    now: datetime | None = None


def auto_send_fire_check(
    session: Session, user: User, account_id: int, *, clock: Callable[[], datetime]
) -> FireCheck:
    """The auto-send handler's own checks before its claim, in its order: the pause, the
    arming, the hold, then the spacing after the last ``message_send`` run. ``clock`` is
    read once, only when the spacing is checked; the handler claims at that time.
    Read-only: the handler and the auto-send outlook both run it."""
    if schedule_paused(session, user, account_id):
        return FireCheck(FireStop.PAUSED)
    if not scheduled_runs_armed(session, user, account_id):
        return FireCheck(FireStop.DISARMED)
    if auto_send_hold(session, user, account_id) is not None:
        return FireCheck(FireStop.HELD)
    now = clock()
    if auto_send_spacing_left(session, user, now) > timedelta(0):
        return FireCheck(FireStop.SPACING, now)
    return FireCheck(None, now)


def _auto_send_handler(
    factory: sessionmaker[Session],
    executor: runs.RunExecutor,
    tasks: TaskRunner,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
) -> JobHandler:
    """The auto-send fire (ADR 0008): claim one due ``auto_send`` step and run it.
    ``settings`` are the file's; the flag and every other value are resolved for the user
    at each fire (#343, #598), never kept from one fire to the next."""

    def auto_send_on(user_id: int) -> bool:
        """The flag in force for the user now, in a read session: off, the fire takes no
        write lock."""
        with session_scope(factory) as session:
            user = session.get(User, user_id)
            return user is not None and ui_settings.auto_send_on(session, user, settings)

    def claim(ctx: JobContext) -> int | str:
        """The claimed run's id, or why nothing was claimed, in fixed words."""
        if not auto_send_on(ctx.user_id):
            return AUTO_SEND_OFF_WORDS
        with session_scope(factory, write=True) as session:
            user = session.get(User, ctx.user_id)
            if user is None:
                return "no such user"
            check = auto_send_fire_check(session, user, ctx.account_id, clock=clock)
            if check.stop is FireStop.PAUSED:
                raise _SchedulePaused
            if check.stop is FireStop.DISARMED:
                raise runs.ScheduledRunsDisarmed("scheduled runs are disarmed")
            if check.stop is FireStop.HELD:
                return "auto-send is held until the open message bubbles are closed"
            if check.stop is FireStop.SPACING:
                return "the spacing since the last auto-send has not passed"
            assert check.now is not None
            now = check.now
            # The Settings page's values (#343), read at each claim like every other reader.
            in_force = ui_settings.resolve(session, user, settings)
            claimed = claim_auto_send(session, user, now=now, settings=in_force)
            if claimed is None:
                return "no auto-send step is ready"
            if not claimed.claimed or claimed.run_id is None:
                return ", ".join(claimed.reasons)
            return claimed.run_id

    async def note(
        ctx: JobContext, *, run_id: int | None = None, reason: str | None = None
    ) -> None:
        # After the decision, in a session of its own (#505): what `schedule status` and the
        # dashboard show as the last check. It never changes what the fire did.
        try:
            fire = LastFire(at=clock(), run_id=run_id, reason=reason)
            await off_loop(note_last_fire, factory, ctx.user_id, ctx.account_id, ctx.kind, fire)
        except Exception:
            log.exception("scheduled auto-send: could not note the last fire")

    async def handle(ctx: JobContext) -> JobOutcome | None:
        if _restarting(tasks, "auto-send"):
            return JobOutcome.RETRY_LATER
        try:
            claimed = await off_loop(claim, ctx)
        except _SchedulePaused:
            log.info("scheduled auto-send not started: the schedule is paused")
            await note(ctx, reason=JobOutcome.PAUSED_AFTER_GATE.value)
            return JobOutcome.PAUSED_AFTER_GATE
        except runs.ScheduledRunsDisarmed:
            log.error("scheduled auto-send reached a disarmed account past the arm gate")
            await note(ctx, reason=JobOutcome.DISARMED_AFTER_GATE.value)
            return JobOutcome.DISARMED_AFTER_GATE
        if isinstance(claimed, str):
            log.info("scheduled auto-send claimed nothing: %s", claimed)
            if claimed != AUTO_SEND_OFF_WORDS:
                # With the flag off the handler only reads the flag, so it notes nothing.
                await note(ctx, reason=claimed)
            return JobOutcome.NOTHING_TO_SEND
        submitted = await _submit_or_refuse(
            factory, executor, tasks, claimed, ctx.user_id, what="auto-send", clock=clock
        )
        if submitted is None:
            await note(ctx, run_id=claimed, reason=REFUSED_RESTARTING)
            return JobOutcome.RETRY_LATER
        task_id, result = submitted
        await note(ctx, run_id=claimed)
        await tasks.wait(task_id)
        if result() is runs.RunOutcome.RETRY_LATER:
            return JobOutcome.RETRY_LATER
        return None

    return handle


#: The stop reason the worker records on a scheduled run it refuses outside active hours
#: (#696). The handler reads it back to tell the scheduler the fire did not run.
REFUSED_OUTSIDE_HOURS: Final = "inactive"


def _refused_outside_hours(factory: sessionmaker[Session], user_id: int, run_id: int) -> bool:
    """Whether the worker refused run ``run_id`` outside active hours (#696): failed with
    the stop reason ``inactive`` before it ran. False for a run or user that is gone."""
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        if user is None:
            return False
        try:
            run = runs.get_run(session, user, run_id)
        except runs.RunNotFound:
            return False
        return run.status is SyncRunStatus.FAILED and run.stop_reason == REFUSED_OUTSIDE_HOURS


def _lost_answers(factory: sessionmaker[Session], user_id: int, run_id: int) -> int:
    """How many answers run ``run_id`` lost (#200); 0 for a run or user that is gone."""
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        if user is None:
            return 0
        try:
            return runs.lost_answers(runs.get_run(session, user, run_id))
        except runs.RunNotFound:
            return 0


# --- what `netkeep serve` hands the app --------------------------------------


ExecutorFactory = Callable[[sessionmaker[Session], EventBus], runs.RunExecutor]


@dataclass(frozen=True, slots=True)
class ServeExtractor:
    """The extractor half of ``netkeep serve``: how to build the run executor
    once the app has its database and event bus, and the clock the scheduler reads.

    ``netkeep.worker.serve_extractor`` builds the real one (the attach
    provider); a test passes one built on a fake browser and a fake clock and
    drives the same lifespan ``serve`` does. An app built without one
    (``create_app()`` in most tests) starts no scheduler and cannot start runs.
    """

    executor: ExecutorFactory
    clock: Callable[[], datetime] = utcnow
    rng: random.Random | None = None


@dataclass(slots=True)
class ServeScheduler:
    """What the app keeps while it runs: the executor, and the started scheduler."""

    executor: runs.RunExecutor
    scheduler: HeartbeatScheduler

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)


def start_serve_scheduler(
    extractor: ServeExtractor,
    factory: sessionmaker[Session],
    bus: EventBus,
    tasks: TaskRunner,
    settings: LinkedInSettings,
    *,
    campaign_settings: Settings | None = None,
    active_hours: ActiveHoursProvider | None = None,
) -> ServeScheduler:
    """Build the executor and the scheduler for every local user's account, and start it.

    Establishing the schedule writes due times and fires nothing; a fire needs
    a heartbeat, and a fire on a disarmed account is skipped by the arm gate
    (``build_scheduler``'s default, never turned off here). The scheduler runs
    :data:`~netkeep.services.scheduler.SERVED_SCHEDULES` only (the inbox poll
    included since P4-01) with the configured heat gate and active hours, and auto-send
    (ADR 0008), whose handler reads ``[campaigns] linkedin_auto_send`` for each user at
    each fire (#598).

    ``active_hours`` is each user's own window, read at every heartbeat (#464; see
    :func:`user_active_hours`); ``settings.active_hours`` is only the default for a caller
    that passes none.
    """
    executor = extractor.executor(factory, bus)
    _quiet_the_heartbeat()
    start, end = (time.fromisoformat(value) for value in settings.active_hours)
    whole = campaign_settings if campaign_settings is not None else Settings()
    whole = dataclasses.replace(whole, linkedin=settings)
    # Auto-send is always scheduled: its handler reads the flag at each fire (#598).
    schedules = served_schedules(True)
    built = build_scheduler(
        factory,
        _local_accounts(factory),
        registry=serve_registry(factory, executor, tasks, clock=extractor.clock, settings=whole),
        schedules=schedules,
        active_start=start,
        active_end=end,
        active_hours=active_hours,
        heat_settings=settings.heat,
        rng=extractor.rng,
        clock=extractor.clock,
        kind_off=auto_send_off(factory, whole),
    )
    built.start()
    log.info("scheduler started: %s", ", ".join(kind.value for kind in schedules))
    if whole.campaigns.linkedin_auto_send:
        # The value in force at start; the Settings page logs its own when it is saved.
        log.warning(
            "LinkedIn auto-send is on: netkeep clicks Send itself for steps set to Auto-send"
        )
        warning = auto_send_budget_warning(whole)
        if warning is not None:
            log.warning("%s", warning)
    return ServeScheduler(executor=executor, scheduler=built)


def auto_send_off(factory: sessionmaker[Session], base: Settings) -> KindOffProvider:
    """For the scheduler (#598 review): the auto-send kind is off for a user whose flag
    in force is off, read at each call over ``base`` (the file's settings). Every other
    kind is never off."""

    def off(user: User, kind: JobKind) -> bool:
        if kind is not JobKind.AUTO_SEND:
            return False
        with session_scope(factory) as session:
            fresh = session.get(User, user.id)
            return fresh is None or not ui_settings.auto_send_on(session, fresh, base)

    return off


def user_active_hours(factory: sessionmaker[Session], base: Settings) -> ActiveHoursProvider:
    """Each user's active hours as the scheduler reads them (#464): ``base`` (the file's
    settings) with that user's Settings-page values, resolved at each call so an edit
    applies at the next heartbeat. Raises for a user that is gone or a window that does
    not parse; the scheduler then skips that user's fires."""

    def hours(user: User) -> tuple[time, time]:
        with session_scope(factory) as session:
            fresh = session.get(User, user.id)
            if fresh is None:
                raise LookupError(f"no user {user.id}")
            in_force = ui_settings.resolve(session, fresh, base)
        return active_window(in_force)

    return hours


def active_window(settings: Settings) -> tuple[time, time]:
    """``[linkedin] active_hours`` as the scheduler reads them at each heartbeat. Raises
    :class:`ValueError` for a window that does not parse."""
    start, end = (time.fromisoformat(value) for value in settings.linkedin.active_hours)
    return start, end


def seed_served_schedule(
    session: Session,
    user: User,
    settings: LinkedInSettings,
    *,
    now: datetime,
    rng: random.Random | None = None,
) -> list[JobKind]:
    """Seed a due time for each served kind that has none, as arming does (#327).

    ``serve`` startup already gives a missing kind its first due time
    (:func:`~netkeep.services.scheduler.build_scheduler`); arming does it too, so a
    kind added after the schedule was established never waits for a restart. A kind
    that has a due time keeps it. Needs a writer session. Returns the kinds seeded.

    Only an established schedule is filled in. When no served kind has a due time yet
    (``serve`` has never run), this seeds nothing and leaves the whole schedule to
    ``serve``'s first start: seeded here, every due time could have lapsed by then,
    and that first start would read them as downtime and catch them all up at once.
    """
    account = find_account(session, user)
    if account is None:
        return []
    if all(stored_due(session, user, account.id, kind) is None for kind in SERVED_SCHEDULES):
        return []
    start, end = (time.fromisoformat(value) for value in settings.active_hours)
    seeded = seed_missing_kinds(
        session,
        user,
        account.id,
        now=now,
        schedules=SERVED_SCHEDULES,
        rng=rng if rng is not None else random.Random(),  # noqa: S311 -- jitter, not crypto
        tz=user.timezone,
        active_start=start,
        active_end=end,
    )
    for kind, result in seeded.items():
        log.info("scheduler: seeded %s for account %d, due %s", kind.value, account.id, result.due)
    return list(seeded)


def _local_accounts(factory: sessionmaker[Session]) -> Callable[[], list[tuple[User, int]]]:
    """Every local user with their account id, read fresh at each call."""

    def accounts() -> list[tuple[User, int]]:
        with session_scope(factory) as session:
            users = local_users(session)
            found: list[tuple[User, int]] = []
            for user in users:
                account = find_account(session, user)
                if account is not None:
                    found.append((user, account.id))
            return found

    return accounts


class _BusyHeartbeat(logging.Filter):
    """Drops APScheduler's "skipped: maximum number of running instances" warning.

    The heartbeat awaits a run to its end on purpose (one fire at a time, see
    ``services.scheduler``), so during an hour of enrichment every minute's tick
    finds it still running. That is the design working, not a warning.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "maximum number of running instances reached" not in record.getMessage()


def _quiet_the_heartbeat() -> None:
    """Once per process: the filter above, and APScheduler's per-tick INFO lines
    ("Running job", "executed successfully") down to WARNING."""
    scheduler_log = logging.getLogger("apscheduler.scheduler")
    if not any(isinstance(item, _BusyHeartbeat) for item in scheduler_log.filters):
        scheduler_log.addFilter(_BusyHeartbeat())
    executors_log = logging.getLogger("apscheduler.executors")
    if executors_log.level < logging.WARNING:
        executors_log.setLevel(logging.WARNING)
