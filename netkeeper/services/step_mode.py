"""Switch a running campaign's LinkedIn step between Prefill and Auto-send (#536).

A LinkedIn step's mode decides who sends its messages: ``prefill`` waits for a person,
who watches netkeeper type the message and clicks Send, and ``auto_send`` lets the
scheduler click Send (ADR 0008). This module lets a person change it on an ``active``
or ``paused`` campaign. It changes what can send, so it is built around three rules.

**A switch never sends anything by itself.** It writes the step's mode, the
enrollments that keep their mode (below), and one history row, and nothing else: no
message, no due time, no run, no claim, no budget, no hold. Whatever sends afterward
goes through the claim and the run it always did, with every gate they apply
(``[campaigns] linkedin_auto_send``, the review's approvals behind ``active``, the
sending hours, the guards, the one open prefill, LinkedIn's active hours, the channel
guard and its budgets, the inbox poll, the auto-send hold, the spacing, and the
recheck before the click: :mod:`netkeeper.services.linkedin_steps`,
:mod:`netkeeper.services.message_send`).

**Prefill to Auto-send: work already queued keeps Prefill.** An enrollment already
queued for the step when it switches keeps the step as a prefill
(``enrollments.prefill_kept_step_id``, :func:`~netkeeper.services.linkedin_steps.effective_mode`):
it is live (``active`` or ``paused``), the step is its next one, the step has no
message for it yet, and it is either due or parked for the step with a reason on
file (Try again, blocked). The auto-send claim never takes it; a person prefills it
as before. A message that exists keeps the mode it was made with: a claimed or
running prefill is a manual run, which never clicks Send, and a message already
prefilled waits for the person, since the step already has a message for that
enrollment and is never claimed again. Only enrollments that reach the step later
use Auto-send. The switch needs ``confirm``: it is the person saying yes to
:data:`AUTO_SEND_CONFIRM`.

**Auto-send to Prefill: nothing more is auto-sent.** Every enrollment of the step
waits for a person from then on, including any that kept Prefill from an earlier
switch. An auto-send claimed or running stops at its next live check: before it
opens anything (:func:`~netkeeper.services.message_send.prepare` and the run's gates),
the claim is given back; after it typed, before Send, the recheck stops the click and
the message waits as a prefill. A Send already clicked is done. This direction does
not keep the old mode for work in flight, because keeping it would mean an auto-send
the person just turned off still clicks Send. It needs no confirm.

Switching is allowed while ``[campaigns] linkedin_auto_send`` is off: the step is
then an ``auto_send`` step that sends nothing until the flag is on, and the history
row records that the flag was off. Every function that writes needs a writer session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, and_, exists, func, or_
from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    StepMode,
    StepModeChange,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services.campaigns import CampaignConflict, CampaignNotFound, InvalidCampaign

log = logging.getLogger(__name__)

SWITCHABLE: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)
"""A campaign whose LinkedIn steps switch mode here: activated, not over. A draft's
steps change in the builder."""

LINKEDIN_MODES: Final[frozenset[StepMode]] = frozenset({StepMode.PREFILL, StepMode.AUTO_SEND})

LIVE: Final[tuple[EnrollmentStatus, ...]] = (EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED)
"""An enrollment that can still be sent its next step."""

AUTO_SEND_CONFIRM: Final = (
    "netkeeper sends this step's messages itself, within your active hours, spacing and budget."
)
"""What a switch to Auto-send changes, as the confirm says it (#536)."""

HISTORY_MAX: Final = 200
"""The most switches :func:`history` lists for one campaign, newest first."""


class ConfirmNeeded(InvalidCampaign):
    """A switch to Auto-send without ``confirm``."""


@dataclass(frozen=True, slots=True)
class ModePreview:
    """What switching ``step_id`` now would change. Reads only."""

    campaign_id: int
    campaign_status: CampaignStatus
    step_id: int
    position: int
    mode: StepMode
    auto_send_on: bool
    """Whether ``[campaigns] linkedin_auto_send`` is on: off, an ``auto_send`` step sends
    nothing."""
    queued: int
    """Enrollments queued for the step now: on a switch to Auto-send, they keep Prefill."""
    scheduled: int
    """Enrollments waiting for the step but not queued yet (due later, or waiting on an
    earlier step): on a switch to Auto-send, they are sent on their own."""
    kept_prefill: int
    """Enrollments that keep the step as a prefill from an earlier switch, still waiting."""
    claimed: int
    """The step's messages claimed and not yet recorded (``scheduled``): a prefill or an
    auto-send under way."""
    prefilled: int
    """The step's messages typed into LinkedIn and waiting to be seen sent."""
    refusal: str | None
    """Why the step cannot switch; None when it can."""


@dataclass(frozen=True, slots=True)
class ModeSwitch:
    campaign_id: int
    step_id: int
    position: int
    from_mode: StepMode
    to_mode: StepMode
    changed: bool
    """False when the step already had ``to_mode``: nothing was written."""
    kept_prefill: int
    """Enrollments that kept the step as a prefill, on a switch to Auto-send."""
    auto_send_on: bool
    changed_at: datetime


def _require_writer(session: Session, what: str) -> None:
    if not is_writer(session):
        raise RuntimeError(f"{what} needs a writer session (session_scope(..., write=True))")


def _campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    campaign = session.scalars(
        scoped(user, Campaign)
        .where(Campaign.id == campaign_id)
        .execution_options(populate_existing=True)
    ).first()
    if campaign is None:
        raise CampaignNotFound(f"no campaign {campaign_id}")
    return campaign


def _steps(session: Session, user: User, campaign_id: int) -> list[CampaignStep]:
    return list(
        session.scalars(
            scoped(user, CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position)
            .execution_options(populate_existing=True)
        )
    )


def _step(steps: list[CampaignStep], campaign_id: int, step_id: int) -> CampaignStep:
    step = next((s for s in steps if s.id == step_id), None)
    if step is None:
        raise CampaignNotFound(f"no step {step_id} in campaign {campaign_id}")
    return step


def _refusal(campaign: Campaign, step: CampaignStep) -> str | None:
    if campaign.status not in SWITCHABLE:
        return (
            f"campaign {campaign.id} is {campaign.status}; only an active or paused campaign's"
            " step switches mode here (a draft's steps change in the builder)"
        )
    if step.channel is not TemplateChannel.LINKEDIN:
        return f"step {step.position} is an email step; only a LinkedIn step has this mode"
    return None


def _no_message_for(step: CampaignStep) -> ColumnElement[bool]:
    """SQL: the enrollment has no outbound message of ``step``, of any status."""
    return ~exists().where(
        Message.user_id == step.user_id,
        Message.enrollment_id == Enrollment.id,
        Message.step_id == step.id,
        Message.direction == MessageDirection.OUT,
    )


def _waiting_clauses(
    step: CampaignStep, steps: list[CampaignStep]
) -> tuple[ColumnElement[bool], ...]:
    """SQL: a live enrollment whose next step is ``step``, with no message of it yet."""
    earlier = [s.position for s in steps if s.position < step.position]
    previous = max(earlier) if earlier else None
    return (
        Enrollment.campaign_id == step.campaign_id,
        Enrollment.status.in_(LIVE),
        Enrollment.current_step.is_(None)
        if previous is None
        else Enrollment.current_step == previous,
        _no_message_for(step),
    )


def _due_or_parked(now: datetime) -> ColumnElement[bool]:
    """SQL: due by ``now``, or parked for its step with a reason on file (Try again,
    blocked by its template). One parked waiting on an earlier step's message has no
    reason on file."""
    return or_(
        Enrollment.next_action_at <= now,
        and_(Enrollment.next_action_at.is_(None), Enrollment.not_sent_error.is_not(None)),
    )


def _queued_clauses(
    step: CampaignStep, steps: list[CampaignStep], now: datetime
) -> tuple[ColumnElement[bool], ...]:
    """SQL: queued for ``step`` now: waiting for it (:func:`_waiting_clauses`), and due or
    parked for it (:func:`_due_or_parked`)."""
    return (*_waiting_clauses(step, steps), _due_or_parked(now))


def _scheduled_clauses(
    step: CampaignStep, steps: list[CampaignStep], now: datetime
) -> tuple[ColumnElement[bool], ...]:
    """SQL: enrolled and waiting for ``step``, but not queued for it yet: due later, or
    parked on an earlier step's message. On a switch to Auto-send these are sent on
    their own when they reach it."""
    # The complement of _due_or_parked written out: NOT over a NULL comparison is NULL.
    return (
        *_waiting_clauses(step, steps),
        or_(
            Enrollment.next_action_at > now,
            and_(Enrollment.next_action_at.is_(None), Enrollment.not_sent_error.is_(None)),
        ),
    )


def _count(session: Session, user: User, *clauses: ColumnElement[bool]) -> int:
    return (
        session.scalar(scoped(user, Enrollment).with_only_columns(func.count()).where(*clauses))
        or 0
    )


def _messages(session: Session, user: User, step: CampaignStep, status: MessageStatus) -> int:
    clauses: list[ColumnElement[bool]] = [
        Message.step_id == step.id,
        Message.direction == MessageDirection.OUT,
        Message.status == status,
    ]
    if status is MessageStatus.PREFILLED:
        clauses.append(Message.send_clicked_at.is_(None))
    return (
        session.scalar(scoped(user, Message).with_only_columns(func.count()).where(*clauses)) or 0
    )


def kept_prefill(session: Session, user: User, step: CampaignStep) -> int:
    """Live enrollments that keep ``step`` as a prefill and have not had it yet. Read-only."""
    return _count(
        session,
        user,
        Enrollment.prefill_kept_step_id == step.id,
        Enrollment.status.in_(LIVE),
        func.coalesce(Enrollment.current_step, 0) < step.position,
    )


def preview(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    *,
    now: datetime,
    auto_send_on: bool,
) -> ModePreview:
    """What switching the step now would change, and whether it may switch. Reads only."""
    campaign = _campaign(session, user, campaign_id)
    steps = _steps(session, user, campaign_id)
    step = _step(steps, campaign_id, step_id)
    return ModePreview(
        campaign_id=campaign.id,
        campaign_status=campaign.status,
        step_id=step.id,
        position=step.position,
        mode=step.mode,
        auto_send_on=auto_send_on,
        queued=_count(session, user, *_queued_clauses(step, steps, now)),
        scheduled=_count(session, user, *_scheduled_clauses(step, steps, now)),
        kept_prefill=kept_prefill(session, user, step),
        claimed=_messages(session, user, step, MessageStatus.SCHEDULED),
        prefilled=_messages(session, user, step, MessageStatus.PREFILLED),
        refusal=_refusal(campaign, step),
    )


def switch(
    session: Session,
    user: User,
    campaign_id: int,
    step_id: int,
    *,
    mode: StepMode,
    confirm: bool,
    now: datetime,
    auto_send_on: bool,
) -> ModeSwitch:
    """Set a LinkedIn step's mode: see the module docstring.

    Refused (:class:`~netkeeper.services.campaigns.CampaignConflict`) for a campaign that
    is not active or paused, (:class:`~netkeeper.services.campaigns.InvalidCampaign`) for
    an email step or a mode that is not a LinkedIn one, and (:class:`ConfirmNeeded`) for a
    switch to ``auto_send`` without ``confirm``. Switching to the mode the step has
    changes nothing and records nothing.
    """
    _require_writer(session, "switch")
    campaign = _campaign(session, user, campaign_id)
    steps = _steps(session, user, campaign_id)
    step = _step(steps, campaign_id, step_id)
    refusal = _refusal(campaign, step)
    if refusal is not None:
        if step.channel is not TemplateChannel.LINKEDIN:
            raise InvalidCampaign(refusal)
        raise CampaignConflict(refusal)
    if mode not in LINKEDIN_MODES:
        raise InvalidCampaign(f"a LinkedIn step is prefill or auto_send, not {mode.value}")
    before = step.mode
    if mode is before:
        return ModeSwitch(
            campaign.id, step.id, step.position, before, mode, False, 0, auto_send_on, now
        )
    if mode is StepMode.AUTO_SEND and not confirm:
        raise ConfirmNeeded(f"confirm the switch to auto_send: {AUTO_SEND_CONFIRM}")

    kept = 0
    if mode is StepMode.AUTO_SEND:
        # Work already queued keeps Prefill; only enrollments that reach the step later
        # are auto-sent. Never a due time, a message or a run: nothing sends from here.
        for enrollment in session.scalars(
            scoped(user, Enrollment).where(*_queued_clauses(step, steps, now))
        ):
            enrollment.prefill_kept_step_id = step.id
            kept += 1
    else:
        # Nothing more is auto-sent: what kept Prefill from an earlier switch is moot.
        for enrollment in session.scalars(
            scoped(user, Enrollment).where(Enrollment.prefill_kept_step_id == step.id)
        ):
            enrollment.prefill_kept_step_id = None
    step.mode = mode
    session.add(
        StepModeChange(
            user_id=user.id,
            campaign_id=campaign.id,
            step_id=step.id,
            position=step.position,
            from_mode=before,
            to_mode=mode,
            changed_at=now,
            changed_by=user.id,
            kept_prefill=kept,
            auto_send_on=auto_send_on,
        )
    )
    session.flush()
    log.info(
        "campaign %d: step %d switched from %s to %s; %d queued enrollments keep prefill%s",
        campaign.id,
        step.position,
        before.value,
        mode.value,
        kept,
        "" if auto_send_on else " (auto-send is off)",
    )
    return ModeSwitch(
        campaign.id, step.id, step.position, before, mode, True, kept, auto_send_on, now
    )


def history(session: Session, user: User, campaign_id: int) -> list[StepModeChange]:
    """The campaign's mode switches, newest first, at most :data:`HISTORY_MAX`. Read-only."""
    _campaign(session, user, campaign_id)
    return list(
        session.scalars(
            scoped(user, StepModeChange)
            .where(StepModeChange.campaign_id == campaign_id)
            .order_by(StepModeChange.changed_at.desc(), StepModeChange.id.desc())
            .limit(HISTORY_MAX)
        )
    )
