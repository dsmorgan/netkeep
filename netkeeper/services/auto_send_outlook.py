"""When the next auto-send goes out, or why it waits (#505). Read-only.

LinkedIn auto-send (ADR 0008) sends from the scheduler's auto-send job: every
:data:`~netkeeper.services.scheduler.AUTO_SEND_INTERVAL` it fires, and each fire claims at
most one due ``auto_send`` step. Between the job's due time and a Send click sit a dozen
checks in three places: the scheduler's gate, the job's handler, and the claim. This
module answers, for a person, when the job next fires, whether that fire would claim a
step, and if not, the first check that stops it.

**One source of truth.** Nothing here re-implements a check. The outlook calls the
functions the fire itself calls, in the fire's order, at the fire's time:

1. ``[campaigns] linkedin_auto_send``, as ``netkeeper serve`` reads it, and whether the
   job has a due time at all;
2. the scheduler's gate (:func:`netkeeper.services.scheduler.gate_skip_reason`): disarmed,
   paused, the session flag, heat;
3. the handler's checks (:func:`netkeeper.services.scheduled_runs.auto_send_fire_check`):
   the hold, the spacing after the last ``message_send`` run;
4. the claim's checks before any step
   (:func:`netkeeper.services.linkedin_steps.auto_send_precheck`): the hold, an
   interrupted auto-send, heat, today's ``li_messages_auto`` budget;
5. the steps the claim tries
   (:func:`netkeeper.services.linkedin_steps.auto_send_ready_statement`), and the sending
   hours that hold each one;
6. the claim's checks about the user and the browser
   (:func:`netkeeper.services.linkedin_steps.user_refusal`): an open prefill, active hours,
   the channel, the inbox poll.

They read the database as it is now, at the job's next due time (or now, when that has
passed). A step's own checks that come after its sending hours (a reply, the guards, the
cadence after the previous step) are not read here: the claim reads them for one step at
a time, and may still pass one step over for the next.

The last fire's outcome is the scheduler's own record
(:func:`netkeeper.services.scheduler.last_fire`). Nothing here writes, and nothing here
touches a browser.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.campaigns import schedule
from netkeeper.config import Settings
from netkeeper.linkedin import pacing
from netkeeper.models import Campaign, Enrollment, User
from netkeeper.scoping import get_scoped
from netkeeper.services import budgets, inbox_hold, sending_hours, ui_settings
from netkeeper.services import campaign_engine as engine
from netkeeper.services.linkedin_accounts import account_id_for
from netkeeper.services.linkedin_steps import (
    AUTO_SEND_HOLD_INTERRUPTED,
    AutoSendStop,
    Refusal,
    auto_send_hold,
    auto_send_precheck,
    auto_send_ready_statement,
    user_refusal,
)
from netkeeper.services.posture import heat_status
from netkeeper.services.scheduled_runs import FireStop, auto_send_fire_check, auto_send_spacing
from netkeeper.services.scheduler import (
    JobKind,
    LastFire,
    gate_skip_reason,
    last_fire,
    stored_due,
)


class Wait(enum.StrEnum):
    """Why the next auto-send fire would claim nothing: the first check that stops it."""

    AUTO_SEND_OFF = "auto_send_off"
    NOT_SCHEDULED = "not_scheduled"
    DISARMED = "disarmed"
    PAUSED = "paused"
    SESSION_FLAGGED = "session_flagged"
    HEAT = "heat"
    HELD = "held"
    SPACING = "spacing"
    BUDGET_SPENT = "budget_spent"
    NO_STEP_DUE = "no_step_due"
    BAD_SENDING_HOURS = "bad_sending_hours"
    SENDING_HOURS = "sending_hours"
    PREFILL_OPEN = "prefill_open"
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"
    BAD_ACTIVE_HOURS = "bad_active_hours"
    NOT_LOGGED_IN = "not_logged_in"
    BROWSER_UNHEALTHY = "browser_unhealthy"
    OUT_OF_BUDGET = "out_of_budget"
    INBOX_STALE = "inbox_stale"


#: The claim's refusal codes about the user, as waits.
_REFUSALS: Final[dict[str, Wait]] = {
    Refusal.PREFILL_OPEN: Wait.PREFILL_OPEN,
    Refusal.OUTSIDE_ACTIVE_HOURS: Wait.OUTSIDE_ACTIVE_HOURS,
    Refusal.BAD_ACTIVE_HOURS: Wait.BAD_ACTIVE_HOURS,
    "browser_unknown": Wait.NOT_LOGGED_IN,
    "browser_unhealthy": Wait.BROWSER_UNHEALTHY,
    "browser_out_of_budget": Wait.OUT_OF_BUDGET,
    Refusal.INBOX_STALE: Wait.INBOX_STALE,
}

#: The scheduler gate's skip reasons an auto-send fire can meet, as waits.
_GATE: Final[dict[str, Wait]] = {
    "disarmed": Wait.DISARMED,
    "paused": Wait.PAUSED,
    "session_flagged": Wait.SESSION_FLAGGED,
    "heat": Wait.HEAT,
}


@dataclass(frozen=True, slots=True)
class AutoSendOutlook:
    """What the next auto-send fire would do.

    ``next_fire`` is the job's stored due time (``None`` when it has none);
    ``checked_at`` is the time the checks were read for: ``next_fire``, or now once that
    has passed. ``wait`` is ``None`` when the fire would claim a step, and
    ``enrollment_id`` is then the one it tries first. ``until`` is when the wait can end,
    where a check says so: active hours open, the spacing ends, the budget resets, heat
    cools, the sending hours open. ``spacing_earliest`` and ``spacing_latest`` bound the
    spacing whatever its draw. ``hold_reason`` is the hold's own words. ``inbox_age_hours``
    and ``inbox_limit_hours`` say how old the newest complete inbox poll is (``None``:
    none has completed) and how old it may be. ``detail`` is the check's own line for a
    person, when it has one. No field holds a contact's name or a message.
    """

    next_fire: datetime | None
    checked_at: datetime
    wait: Wait | None
    until: datetime | None = None
    enrollment_id: int | None = None
    spacing_earliest: datetime | None = None
    spacing_latest: datetime | None = None
    hold_reason: str | None = None
    inbox_age_hours: float | None = None
    inbox_limit_hours: float | None = None
    detail: str | None = None
    last_fire: LastFire | None = None

    @property
    def would_send(self) -> bool:
        return self.wait is None


def outlook(session: Session, user: User, *, now: datetime, settings: Settings) -> AutoSendOutlook:
    """The next auto-send fire's outlook for ``user``. ``settings`` are the file's, as
    ``netkeeper serve`` holds them; the claim's own reads resolve the Settings page's
    values over them, as the handler does. Read-only."""
    account_id = account_id_for(session, user)
    next_fire = stored_due(session, user, account_id, JobKind.AUTO_SEND)
    at = next_fire if next_fire is not None and next_fire > now else now
    last = last_fire(session, user, account_id, JobKind.AUTO_SEND)

    blank = AutoSendOutlook(next_fire=next_fire, checked_at=at, wait=None, last_fire=last)

    def waits(wait: Wait | None, **fields: object) -> AutoSendOutlook:
        return replace(blank, wait=wait, **fields)  # type: ignore[arg-type]

    # 1. The flag, as serve reads it: off, the job has no handler and no fire.
    if not settings.campaigns.linkedin_auto_send:
        return waits(Wait.AUTO_SEND_OFF)
    if next_fire is None:
        return waits(Wait.NOT_SCHEDULED)

    # 2. The scheduler's gate, with the heat settings serve passes it.
    skipped = gate_skip_reason(
        session, user, account_id, JobKind.AUTO_SEND, now=at, heat_settings=settings.linkedin.heat
    )
    if skipped is not None:
        if skipped == "heat":
            return waits(Wait.HEAT, until=_heat_resumes(session, user, account_id, at, settings))
        return waits(_GATE.get(skipped, Wait.DISARMED), detail=skipped)

    # 3. The handler's own checks.
    check = auto_send_fire_check(session, user, account_id, clock=lambda: at)
    if check.stop is FireStop.PAUSED:
        return waits(Wait.PAUSED)
    if check.stop is FireStop.DISARMED:
        return waits(Wait.DISARMED)
    if check.stop is FireStop.HELD:
        hold = auto_send_hold(session, user, account_id)
        return waits(Wait.HELD, hold_reason=None if hold is None else hold.reason)
    if check.stop is FireStop.SPACING:
        spacing = auto_send_spacing(session, user)
        return waits(
            Wait.SPACING,
            until=None if spacing is None else spacing.ends_at,
            spacing_earliest=None if spacing is None else spacing.earliest,
            spacing_latest=None if spacing is None else spacing.latest,
        )

    # 4. The claim's checks before any step, with the Settings page's values (#343).
    in_force = ui_settings.resolve(session, user, settings)
    stopped = auto_send_precheck(session, user, now=at, settings=in_force)
    if stopped is not None:
        if stopped.stop is AutoSendStop.OFF:
            return waits(Wait.AUTO_SEND_OFF)
        if stopped.stop is AutoSendStop.HELD:
            hold = auto_send_hold(session, user, account_id)
            return waits(Wait.HELD, hold_reason=None if hold is None else hold.reason)
        if stopped.stop is AutoSendStop.INTERRUPTED:
            # The claim writes this hold when it meets the interrupted run.
            return waits(Wait.HELD, hold_reason=AUTO_SEND_HOLD_INTERRUPTED)
        if stopped.stop is AutoSendStop.HEAT:
            return waits(Wait.HEAT, until=_heat_resumes(session, user, account_id, at, in_force))
        return waits(Wait.BUDGET_SPENT, until=budgets.day_resets_at(user, at))

    # 5. The steps the claim tries, and the sending hours that hold each one.
    enrollments = session.scalars(auto_send_ready_statement(user, at)).all()
    if not enrollments:
        return waits(Wait.NO_STEP_DUE)
    try:
        slots = engine.slots_for(in_force, user)
        hours = sending_hours.read(session, user)
    except schedule.ScheduleError as exc:
        return waits(Wait.BAD_SENDING_HOURS, detail=str(exc))
    candidate, opens = _first_unheld(session, user, enrollments, at, slots, hours)
    if candidate is None:
        return waits(Wait.SENDING_HOURS, until=opens)

    # 6. The claim's checks about the user and the browser.
    refused = user_refusal(session, user, now=at, settings=in_force, auto=True)
    if refused is not None:
        wait = _REFUSALS.get(refused.reasons[0], Wait.BROWSER_UNHEALTHY)
        if wait is Wait.OUTSIDE_ACTIVE_HOURS:
            return waits(wait, until=_window_opens(in_force, at), detail=refused.detail)
        if wait is Wait.INBOX_STALE:
            polled = inbox_hold.last_complete_poll(session, user, now=at)
            return waits(
                wait,
                inbox_age_hours=(
                    None if polled is None else max((at - polled).total_seconds(), 0) / 3600
                ),
                inbox_limit_hours=inbox_hold.stale_after().total_seconds() / 3600,
            )
        if wait is Wait.OUT_OF_BUDGET:
            return waits(wait, until=budgets.day_resets_at(user, at), detail=refused.detail)
        return waits(wait, detail=refused.detail)
    return waits(None, enrollment_id=candidate.id)


def _first_unheld(
    session: Session,
    user: User,
    enrollments: Sequence[Enrollment],
    at: datetime,
    slots: schedule.Suggested,
    hours: schedule.SendingHours,
) -> tuple[Enrollment | None, datetime | None]:
    """The first enrollment the sending hours let go at ``at``, the claim's own rule; or,
    when they hold every one, the soonest time one may go."""
    soonest: datetime | None = None
    for enrollment in enrollments:
        due = enrollment.next_action_at
        campaign = get_scoped(session, user, Campaign, enrollment.campaign_id)
        if due is None or campaign is None:
            continue
        held = schedule.hold(
            due,
            at,
            slots=slots,
            hours=hours,
            starts_at=campaign.starts_at if campaign.start_chosen else None,
            first_step=enrollment.current_step is None,
        )
        if held is None:
            return enrollment, None
        soonest = held if soonest is None else min(soonest, held)
    return None, soonest


def _window_opens(settings: Settings, at: datetime) -> datetime | None:
    """When ``[linkedin] active_hours`` next open after ``at``; ``None`` for hours that
    do not parse."""
    try:
        start = time.fromisoformat(settings.linkedin.active_hours[0])
    except (ValueError, IndexError):
        return None
    return pacing.next_window_start(at, settings.linkedin.timezone, start=start)


def _heat_resumes(
    session: Session, user: User, account_id: int, at: datetime, settings: Settings
) -> datetime | None:
    """When heat falls under its skip threshold again, as the heat card says it."""
    return heat_status(session, user, account_id, now=at, settings=settings.linkedin).resumes_at


# --- in words, for `netkeeper linkedin schedule status` -------------------------------------

TimeFormat = Callable[[datetime], str]

#: Each wait in plain words; ``{until}`` is filled in where the wait has an end.
WORDS: Final[dict[Wait, str]] = {
    Wait.AUTO_SEND_OFF: (
        "auto-send is off ([campaigns] linkedin_auto_send in config.toml), so nothing sends"
        " on its own"
    ),
    Wait.NOT_SCHEDULED: (
        "the auto-send check isn't scheduled yet; `netkeeper serve` schedules it when it starts"
    ),
    Wait.DISARMED: "scheduled runs are disarmed, so the check sends nothing",
    Wait.PAUSED: "the schedule is paused, so the check sends nothing",
    Wait.SESSION_FLAGGED: (
        "the LinkedIn session is flagged (a checkpoint or a login wall), so nothing runs"
        " until it's cleared"
    ),
    Wait.HEAT: "heat is at its skip threshold",
    Wait.HELD: "auto-send is held",
    Wait.SPACING: "it waits out the spacing after the last LinkedIn message run",
    Wait.BUDGET_SPENT: "today's auto-send budget (li_messages_auto) is spent",
    Wait.NO_STEP_DUE: "no auto-send step is due",
    Wait.BAD_SENDING_HOURS: "your sending hours can't be read; fix them in Settings",
    Wait.SENDING_HOURS: "your sending hours hold every due auto-send step",
    Wait.PREFILL_OPEN: "another prefill is open: send or discard it first",
    Wait.OUTSIDE_ACTIVE_HOURS: "it's outside your LinkedIn active hours",
    Wait.BAD_ACTIVE_HOURS: "your LinkedIn active hours can't be read; fix them in config.toml",
    Wait.NOT_LOGGED_IN: (
        "nothing says the LinkedIn session is logged in; run `netkeeper preflight` first"
    ),
    Wait.BROWSER_UNHEALTHY: "the LinkedIn session is flagged or heat is too high",
    Wait.OUT_OF_BUDGET: "today's LinkedIn budget for messages or profile visits is spent",
    Wait.INBOX_STALE: "the LinkedIn inbox hasn't been read recently",
}

#: The last fire's stored reasons that are codes, in plain words. The handler's own
#: reasons are already words, and a claim's refusal codes read well enough spaced out.
_LAST_REASONS: Final[dict[str, str]] = {
    "disarmed": WORDS[Wait.DISARMED],
    "paused": WORDS[Wait.PAUSED],
    "session_flagged": WORDS[Wait.SESSION_FLAGGED],
    "heat": WORDS[Wait.HEAT],
    "paused_after_gate": WORDS[Wait.PAUSED],
    "disarmed_after_gate": WORDS[Wait.DISARMED],
}


def describe(seen: AutoSendOutlook, when: TimeFormat) -> str:
    """The outlook in one line: what the next check would do, and when a wait ends."""
    if seen.wait is None:
        return "it would send the oldest due auto-send step"
    words = WORDS[seen.wait]
    if seen.wait is Wait.HELD and seen.hold_reason is not None:
        return (
            f"{words}: {seen.hold_reason}. Fix it in Chrome, then run"
            " `netkeeper linkedin auto-send-resume`"
        )
    if seen.wait is Wait.SPACING and seen.until is not None:
        bounds = ""
        if seen.spacing_earliest is not None and seen.spacing_latest is not None:
            bounds = (
                f" (20 to 45 minutes after that run: between {when(seen.spacing_earliest)}"
                f" and {when(seen.spacing_latest)})"
            )
        return f"{words}; it ends at {when(seen.until)}{bounds}"
    if seen.wait is Wait.INBOX_STALE:
        if seen.inbox_age_hours is None:
            return f"{words}: no inbox poll has completed yet; run `netkeeper linkedin inbox`"
        limit = "" if seen.inbox_limit_hours is None else f" (limit {seen.inbox_limit_hours:.1f})"
        return f"{words}: the last complete poll was {seen.inbox_age_hours:.1f} hours ago{limit}"
    if seen.until is not None:
        ends = {
            Wait.HEAT: "it cools by",
            Wait.BUDGET_SPENT: "it resets at",
            Wait.OUT_OF_BUDGET: "it resets at",
            Wait.SENDING_HOURS: "until",
            Wait.OUTSIDE_ACTIVE_HOURS: "they next open at",
        }.get(seen.wait, "until")
        return f"{words}; {ends} {when(seen.until)}"
    return words


def describe_last(last: LastFire, when: TimeFormat) -> str:
    """The last fire in one line."""
    if last.run_id is not None:
        return f"{when(last.at)}: started run {last.run_id}"
    reason = last.reason or "no reason recorded"
    return f"{when(last.at)}: sent nothing, {_LAST_REASONS.get(reason, reason.replace('_', ' '))}"
