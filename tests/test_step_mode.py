"""A running campaign's LinkedIn step switches between Prefill and Auto-send (#536).

What this file holds above all: a switch never sends anything by itself. Prefill to
Auto-send leaves every contact already queued for the step on Prefill, and every message
that exists keeps the mode it was made with; Auto-send to Prefill stops every auto-send
that has not clicked Send. Every gate an auto-send passes still applies after a switch.

No test here touches a browser, LinkedIn, or the network.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import NOW
from fastapi import FastAPI
from inbox_fakes import record_poll
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker
from test_auto_send import AUTO, CONTACT, Auto, _given_back_unopened, _last_key_does, _no_send
from test_linkedin_steps import LINKEDIN, Lane, make_lane
from typer.testing import CliRunner

from netkeep import migrations
from netkeep.cli import app as cli
from netkeep.db import database_url, make_engine, make_session_factory, session_scope
from netkeep.linkedin.messaging import MessageOutcomeKind
from netkeep.models import (
    CampaignStatus,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageStatus,
    StepMode,
    StepModeChange,
    SyncRun,
    SyncRunKind,
    TemplateChannel,
    User,
    UserKind,
)
from netkeep.scoping import get_scoped, install_scope_guard, scoped, unscoped
from netkeep.services import linkedin_steps, message_send, step_mode
from netkeep.services.budgets import ActionClass
from netkeep.services.campaigns import CampaignConflict, CampaignNotFound, InvalidCampaign
from netkeep.services.linkedin_accounts import arm_scheduled_runs
from netkeep.services.linkedin_steps import Refusal, claim_auto_send
from netkeep.services.step_mode import ConfirmNeeded
from netkeep.services.users import ensure_local_user

CSRF = {"X-Netkeep-Client": "1"}
OFF = replace(AUTO, campaigns=replace(AUTO.campaigns, linkedin_auto_send=False))
LATER = NOW + timedelta(days=1)


@pytest.fixture
def lane(session_factory: sessionmaker[Session]) -> Lane:
    """Two LinkedIn steps, both Prefill, auto-send on in config and scheduled runs armed."""
    made = make_lane(session_factory, channels=(LINKEDIN, LINKEDIN), settings=AUTO)
    made.write(lambda s, u: arm_scheduled_runs(s, u, now=NOW - timedelta(days=1)))
    return made


def _step(lane: Lane, position: int = 1) -> CampaignStep:
    def read(session: Session, user: User) -> CampaignStep:
        return session.scalars(
            scoped(user, CampaignStep).where(
                CampaignStep.campaign_id == lane.campaign_id, CampaignStep.position == position
            )
        ).one()

    return lane.read(read)


def _switch(
    lane: Lane,
    mode: StepMode,
    *,
    position: int = 1,
    confirm: bool = True,
    now: Any = NOW,
    on: bool = True,
) -> step_mode.ModeSwitch:
    step_id = _step(lane, position).id
    return lane.write(
        lambda s, u: step_mode.switch(
            s,
            u,
            lane.campaign_id,
            step_id,
            mode=mode,
            confirm=confirm,
            now=now,
            auto_send_on=on,
        )
    )


def _history(lane: Lane) -> list[StepModeChange]:
    return lane.read(lambda s, u: step_mode.history(s, u, lane.campaign_id))


def _snapshot(lane: Lane) -> dict[str, Any]:
    """Everything a send could come from: due times, messages, runs."""

    def read(session: Session, user: User) -> dict[str, Any]:
        return {
            "due": sorted(
                (e.id, e.next_action_at, e.status, e.current_step)
                for e in session.scalars(scoped(user, Enrollment))
            ),
            "messages": sorted(
                (m.id, m.status, m.send_clicked_at) for m in session.scalars(scoped(user, Message))
            ),
            "runs": sorted(
                r.id
                for r in session.scalars(
                    scoped(user, SyncRun).where(SyncRun.kind == SyncRunKind.MESSAGE_SEND)
                )
            ),
        }

    return lane.read(read)


def _auto_claim(lane: Lane, now: Any = NOW, settings: Any = AUTO) -> Any:
    lane.write(lambda s, u: record_poll(s, u, now - timedelta(minutes=1)))
    return lane.write(lambda s, u: claim_auto_send(s, u, now=now, settings=settings))


def _other_contact(n: int) -> dict[str, Any]:
    return {"li_urn": f"urn:li:fsd_profile:ACoAAModeTest{n}", "li_public_id": f"mode-test-{n}"}


# --- the switch itself ------------------------------------------------------------------


def test_the_confirm_text_is_pinned() -> None:
    """The words a person agrees to (#536), written out here."""
    assert step_mode.AUTO_SEND_CONFIRM == (
        "netkeep sends this step's messages itself, within your active hours, spacing and budget."
    )


def test_a_switch_to_auto_send_needs_the_confirm_and_changes_nothing_without_it(
    lane: Lane,
) -> None:
    lane.enroll(contact=dict(CONTACT))
    before = _snapshot(lane)
    with pytest.raises(ConfirmNeeded, match="confirm"):
        _switch(lane, StepMode.AUTO_SEND, confirm=False)
    assert _step(lane).mode is StepMode.PREFILL
    assert _history(lane) == []
    assert _snapshot(lane) == before


def test_prefill_to_auto_send_sends_nothing_and_keeps_queued_contacts_on_prefill(
    lane: Lane,
) -> None:
    queued = lane.enroll(contact=dict(CONTACT))  # due now: in the queue
    later = lane.enroll(contact=_other_contact(1), next_action_at=LATER)  # not queued yet
    before = _snapshot(lane)

    done = _switch(lane, StepMode.AUTO_SEND)

    assert (done.from_mode, done.to_mode, done.changed, done.kept_prefill) == (
        StepMode.PREFILL,
        StepMode.AUTO_SEND,
        True,
        1,
    )
    assert _step(lane).mode is StepMode.AUTO_SEND
    # Nothing that sends was touched: no message, no run, no due time.
    assert _snapshot(lane) == before
    assert lane.enrollment(queued).prefill_kept_step_id == _step(lane).id
    assert lane.enrollment(later).prefill_kept_step_id is None

    # The scheduler never takes the queued contact; a person still can.
    assert _auto_claim(lane) is None
    manual = lane.claim(queued)
    assert manual.claimed, manual.reasons
    lane.record(manual.message_id or 0, MessageOutcomeKind.PREFILLED)
    lane.finish_runs()
    # The one open prefill still holds the contact that reaches the step later: a gate.
    held = _auto_claim(lane, now=LATER)
    assert held is not None and held.reasons == (Refusal.PREFILL_OPEN,), held
    # Once the person is done with it, that contact is auto-sent.
    message_id = manual.message_id or 0
    lane.write(lambda s, u: linkedin_steps.discard(s, u, message_id, settings=AUTO, now=LATER))
    claim = _auto_claim(lane, now=LATER)
    assert claim is not None and claim.claimed and claim.enrollment_id == later, claim


def test_a_contact_parked_for_try_again_is_queued_and_keeps_prefill(lane: Lane) -> None:
    parked = lane.enroll(
        contact=dict(CONTACT), next_action_at=None, not_sent_error="not_typed: fixed words"
    )
    waiting_on_step_one = lane.enroll(contact=_other_contact(2), next_action_at=None)
    assert _switch(lane, StepMode.AUTO_SEND).kept_prefill == 1
    assert lane.enrollment(parked).prefill_kept_step_id == _step(lane).id
    # Parked with no reason for this step: not in the queue for it.
    assert lane.enrollment(waiting_on_step_one).prefill_kept_step_id is None


def test_a_later_step_keeps_prefill_only_for_contacts_already_queued_for_it(lane: Lane) -> None:
    on_step_two = lane.enroll(contact=dict(CONTACT), current_step=1)
    on_step_one = lane.enroll(contact=_other_contact(3))
    assert _switch(lane, StepMode.AUTO_SEND, position=2).kept_prefill == 1
    assert lane.enrollment(on_step_two).prefill_kept_step_id == _step(lane, 2).id
    assert lane.enrollment(on_step_one).prefill_kept_step_id is None


def test_auto_send_to_prefill_needs_no_confirm_and_nothing_is_auto_sent(lane: Lane) -> None:
    _switch(lane, StepMode.AUTO_SEND)
    enrolled = lane.enroll(contact=dict(CONTACT))  # reaches the step after the switch
    before = _snapshot(lane)

    done = _switch(lane, StepMode.PREFILL, confirm=False)

    assert (done.from_mode, done.to_mode, done.changed) == (
        StepMode.AUTO_SEND,
        StepMode.PREFILL,
        True,
    )
    assert _snapshot(lane) == before
    assert _auto_claim(lane) is None
    assert lane.claim(enrolled).claimed  # a person prefills it


def test_switching_back_to_prefill_clears_what_kept_prefill(lane: Lane) -> None:
    queued = lane.enroll(contact=dict(CONTACT))
    _switch(lane, StepMode.AUTO_SEND)
    assert lane.enrollment(queued).prefill_kept_step_id is not None
    _switch(lane, StepMode.PREFILL)
    assert lane.enrollment(queued).prefill_kept_step_id is None
    # Switched again, it is still queued, so it keeps Prefill again.
    assert _switch(lane, StepMode.AUTO_SEND).kept_prefill == 1
    assert _auto_claim(lane) is None


def test_switching_to_the_mode_the_step_has_records_nothing(lane: Lane) -> None:
    done = _switch(lane, StepMode.PREFILL)
    assert not done.changed
    assert _history(lane) == []


def test_each_switch_is_recorded_without_message_text(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    _switch(lane, StepMode.AUTO_SEND, now=NOW)
    _switch(lane, StepMode.PREFILL, now=NOW + timedelta(minutes=5), on=False)
    rows = _history(lane)
    assert [(r.from_mode, r.to_mode, r.kept_prefill, r.auto_send_on) for r in rows] == [
        (StepMode.AUTO_SEND, StepMode.PREFILL, 0, False),
        (StepMode.PREFILL, StepMode.AUTO_SEND, 1, True),
    ]
    assert all(r.position == 1 and r.changed_by == lane.user_id for r in rows)
    assert rows[1].changed_at == NOW
    # The record holds the switch and nothing a message said.
    columns = {c.key for c in inspect(StepModeChange).columns}
    assert columns == {
        "id",
        "user_id",
        "campaign_id",
        "step_id",
        "position",
        "from_mode",
        "to_mode",
        "changed_at",
        "changed_by",
        "kept_prefill",
        "auto_send_on",
        "created_at",
        "updated_at",
    }


@pytest.mark.parametrize(
    "status",
    [
        CampaignStatus.DRAFT,
        CampaignStatus.REVIEWING,
        CampaignStatus.COMPLETED,
        CampaignStatus.ARCHIVED,
    ],
)
def test_only_an_active_or_paused_campaign_switches(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    lane = make_lane(session_factory, settings=AUTO, status=status)
    with pytest.raises(CampaignConflict, match="only an active or paused"):
        _switch(lane, StepMode.AUTO_SEND)
    assert _step(lane).mode is StepMode.PREFILL


def test_a_paused_campaign_switches_and_its_queued_contacts_keep_prefill(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, settings=AUTO, status=CampaignStatus.PAUSED)
    queued = lane.enroll(contact=dict(CONTACT))
    assert _switch(lane, StepMode.AUTO_SEND).kept_prefill == 1
    assert lane.enrollment(queued).prefill_kept_step_id is not None


def test_an_email_step_or_an_email_mode_is_refused(session_factory: sessionmaker[Session]) -> None:
    lane = make_lane(session_factory, channels=(TemplateChannel.EMAIL, LINKEDIN), settings=AUTO)
    with pytest.raises(InvalidCampaign, match="email step"):
        _switch(lane, StepMode.AUTO_SEND, position=1)
    with pytest.raises(InvalidCampaign, match="prefill or auto_send"):
        _switch(lane, StepMode.SEND, position=2)
    assert _history(lane) == []


def test_another_users_campaign_is_not_found(
    lane: Lane, session_factory: sessionmaker[Session]
) -> None:
    other = make_lane(session_factory, settings=AUTO)
    other_step = _step(other).id
    queued = other.enroll(contact=dict(CONTACT))
    with pytest.raises(CampaignNotFound):
        lane.write(
            lambda s, u: step_mode.switch(
                s,
                u,
                other.campaign_id,
                other_step,
                mode=StepMode.AUTO_SEND,
                confirm=True,
                now=NOW,
                auto_send_on=True,
            )
        )
    with pytest.raises(CampaignNotFound):
        lane.read(lambda s, u: step_mode.history(s, u, other.campaign_id))
    assert _step(other).mode is StepMode.PREFILL
    assert other.enrollment(queued).prefill_kept_step_id is None


def test_a_switch_needs_a_writer_session(lane: Lane) -> None:
    step_id = _step(lane).id
    with pytest.raises(RuntimeError, match="writer"):
        lane.read(
            lambda s, u: step_mode.switch(
                s,
                u,
                lane.campaign_id,
                step_id,
                mode=StepMode.PREFILL,
                confirm=False,
                now=NOW,
                auto_send_on=True,
            )
        )


# --- every gate still applies -----------------------------------------------------------


def test_with_auto_send_off_a_switch_is_allowed_and_nothing_sends(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT), next_action_at=LATER)
    done = _switch(lane, StepMode.AUTO_SEND, on=False)
    assert done.changed and not done.auto_send_on
    assert _history(lane)[0].auto_send_on is False
    assert _auto_claim(lane, now=LATER, settings=OFF) is None
    # The step spends prefills while auto-send is off: a person prefills it.
    assert linkedin_steps.step_budget(_step(lane), OFF) is ActionClass.LI_PREFILLS
    shown = lane.read(
        lambda s, u: step_mode.preview(
            s, u, lane.campaign_id, _step(lane).id, now=NOW, auto_send_on=False
        )
    )
    assert shown.auto_send_on is False and shown.mode is StepMode.AUTO_SEND


def test_a_switched_step_still_passes_the_guards_and_sending_gates(lane: Lane) -> None:
    """The switch adds no shortcut: the claim's own refusals still stop it."""
    enrolled = lane.enroll(contact=dict(CONTACT), next_action_at=LATER)
    _switch(lane, StepMode.AUTO_SEND)

    def no_contact(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrolled)
        assert enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        contact.do_not_contact = True

    lane.write(no_contact)
    claim = _auto_claim(lane, now=LATER)
    assert claim is None or not claim.claimed
    assert lane.messages(enrolled) == []


def test_the_claim_refuses_a_contact_that_kept_prefill(lane: Lane) -> None:
    queued = lane.enroll(contact=dict(CONTACT))
    _switch(lane, StepMode.AUTO_SEND)

    def claim(session: Session, user: User) -> Any:
        return linkedin_steps._Claimer(
            session, user, NOW, AUTO, linkedin_steps.start_auto_send_run, auto=True
        ).claim(queued)

    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    refused = lane.write(claim)
    assert refused.reasons == (Refusal.NOT_AUTO_SEND,)
    assert lane.messages(queued) == []


def test_the_ready_list_says_who_sends_on_their_own(lane: Lane) -> None:
    queued = lane.enroll(contact=dict(CONTACT))
    _switch(lane, StepMode.AUTO_SEND)
    fresh = lane.enroll(contact=_other_contact(4))
    rows, _ = lane.read(
        lambda s, u: linkedin_steps.ready_to_prefill(s, u, now=NOW, settings=AUTO, limit=10)
    )
    modes = {r.enrollment.id: linkedin_steps.effective_mode(r.step, r.enrollment) for r in rows}
    assert modes == {queued: StepMode.PREFILL, fresh: StepMode.AUTO_SEND}


# --- work in flight ----------------------------------------------------------------------


def test_a_prefilled_message_waiting_for_you_is_never_auto_sent_after_a_switch(
    lane: Lane,
) -> None:
    enrolled = lane.enroll(contact=dict(CONTACT))
    message_id = lane.prefill(enrolled)
    _switch(lane, StepMode.AUTO_SEND)
    assert _auto_claim(lane, now=LATER) is None
    message = lane.message(message_id)
    assert message is not None and message.status is MessageStatus.PREFILLED
    assert message.send_clicked_at is None


def test_a_claimed_prefill_stays_a_prefill_after_a_switch(lane: Lane) -> None:
    enrolled = lane.enroll(contact=dict(CONTACT))
    claim = lane.claim(enrolled)
    assert claim.claimed and claim.run_id is not None
    _switch(lane, StepMode.AUTO_SEND)
    prepared = message_send.prepare(
        lane.factory, lane.user_id, claim.run_id, settings=AUTO, clock=lambda: NOW, seed=1
    )
    assert isinstance(prepared, message_send.PreparedPrefill)
    assert not prepared.auto_send and prepared.spec.mode == "prefill"


async def test_switching_to_prefill_before_an_auto_send_opens_anything_stops_it(
    lane: Lane,
) -> None:
    _switch(lane, StepMode.AUTO_SEND)
    a = Auto(lane)
    _switch(lane, StepMode.PREFILL)
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    _given_back_unopened(a, "the step is not an auto-send step")
    # Given back due, it now waits for a person: the scheduler never takes it.
    assert _auto_claim(lane) is None


async def test_switching_to_prefill_after_the_last_key_stops_the_click(lane: Lane) -> None:
    _switch(lane, StepMode.AUTO_SEND)
    a = Auto(lane)
    step_id = _step(lane).id
    _last_key_does(
        a,
        lambda s, u: step_mode.switch(
            s,
            u,
            lane.campaign_id,
            step_id,
            mode=StepMode.PREFILL,
            confirm=False,
            now=NOW,
            auto_send_on=True,
        ),
    )
    await a.execute()
    _no_send(a)
    message = a.message()
    assert message is not None and message.status is MessageStatus.PREFILLED
    assert message.send_clicked_at is None
    assert a.run().notes == "not sent: the step is not an auto-send step"


async def test_a_claimed_auto_send_keeps_auto_send_through_a_switch_and_back(
    lane: Lane,
) -> None:
    """An auto-send claimed before the step goes Prefill and back again is not changed by
    the round trip: it was made an auto-send, and the step is one again when it runs."""
    _switch(lane, StepMode.AUTO_SEND)
    a = Auto(lane)
    _switch(lane, StepMode.PREFILL)
    assert _switch(lane, StepMode.AUTO_SEND).kept_prefill == 0  # it has a message: not queued
    await a.execute()
    message = a.message()
    assert message is not None and message.send_clicked_at is not None


def test_still_wanted_refuses_a_contact_that_kept_prefill(lane: Lane) -> None:
    """Belt and braces: were a kept-prefill contact ever claimed by the scheduler, the
    run's live check would stop it before it opens anything."""
    _switch(lane, StepMode.AUTO_SEND)
    a = Auto(lane)
    step_id = _step(lane).id

    def keep(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, a.enrollment_id)
        assert enrollment is not None
        enrollment.prefill_kept_step_id = step_id

    lane.write(keep)
    why = lane.read(lambda s, u: message_send.still_wanted(s, u, a.run_id, now=NOW))
    assert why == "the step is not an auto-send step"


def test_a_scheduled_run_on_a_kept_contact_is_refused_in_prepare(lane: Lane) -> None:
    """``prepare`` reads the mode for the enrollment: a scheduled run for a contact that
    kept Prefill opens nothing, whatever the step's own mode."""
    _switch(lane, StepMode.AUTO_SEND)
    a = Auto(lane)
    step_id = _step(lane).id

    def keep(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, a.enrollment_id)
        assert enrollment is not None
        enrollment.prefill_kept_step_id = step_id

    lane.write(keep)
    done = message_send.prepare(
        lane.factory, lane.user_id, a.run_id, settings=AUTO, clock=lambda: NOW, seed=1
    )
    assert isinstance(done, message_send.PrefillReport)
    assert a.run().notes == "not started: the step is not an auto-send step"
    assert a.message() is None


def test_a_merge_keeps_the_prefill_mark_of_the_row_that_wins(lane: Lane) -> None:
    from netkeep.crm import identity

    marked = lane.enroll(contact=_other_contact(11))  # active, due: queued at the switch
    survivor = lane.enroll(contact=dict(CONTACT), status=EnrollmentStatus.PENDING)
    assert _switch(lane, StepMode.AUTO_SEND).kept_prefill == 1
    survivor_contact = lane.enrollment(survivor).contact_id
    loser_contact = lane.enrollment(marked).contact_id
    lane.write(lambda s, u: identity.merge(s, u, survivor_contact, loser_contact))

    kept = lane.enrollment(survivor)
    # The loser's row won (active beats pending) and its mark came with it.
    assert kept.status is EnrollmentStatus.ACTIVE and kept.next_action_at == NOW
    assert kept.prefill_kept_step_id == _step(lane).id
    assert _auto_claim(lane) is None


def test_a_merge_keeps_the_mark_for_the_merged_rows_next_step(lane: Lane) -> None:
    from netkeep.crm import identity

    step_one, step_two = _step(lane, 1).id, _step(lane, 2).id

    def mark(enrollment_id: int, step_id: int) -> None:
        def act(session: Session, user: User) -> None:
            row = get_scoped(session, user, Enrollment, enrollment_id)
            assert row is not None
            row.prefill_kept_step_id = step_id

        lane.write(act)

    behind = lane.enroll(contact=_other_contact(12))
    ahead = lane.enroll(contact=dict(CONTACT), current_step=1)
    mark(behind, step_one)
    mark(ahead, step_two)
    survivor_contact = lane.enrollment(ahead).contact_id
    loser_contact = lane.enrollment(behind).contact_id
    lane.write(lambda s, u: identity.merge(s, u, survivor_contact, loser_contact))
    assert lane.enrollment(ahead).prefill_kept_step_id == step_two


# --- the preview, the campaign status --------------------------------------------------


def test_the_preview_counts_what_a_switch_would_change(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    lane.enroll(contact=_other_contact(5), next_action_at=LATER)
    shown = lane.read(
        lambda s, u: step_mode.preview(
            s, u, lane.campaign_id, _step(lane).id, now=NOW, auto_send_on=True
        )
    )
    assert (shown.mode, shown.queued, shown.scheduled, shown.kept_prefill, shown.refusal) == (
        StepMode.PREFILL,
        1,
        1,
        0,
        None,
    )
    _switch(lane, StepMode.AUTO_SEND)
    detail = lane.read(
        lambda s, u: step_mode.preview(
            s, u, lane.campaign_id, _step(lane).id, now=NOW, auto_send_on=True
        )
    )
    assert detail.kept_prefill == 1

    from netkeep.services import campaigns as campaign_service

    status = lane.read(
        lambda s, u: campaign_service.campaign_status(s, u, lane.campaign_id, now=NOW)
    )
    assert [s.kept_prefill for s in status.steps] == [1, 0]


# --- the API -------------------------------------------------------------------------------


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _seed_api(app: FastAPI, *, owner: str = "local", **campaign: Any) -> tuple[int, int, int]:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session) if owner == "local" else factories.make_user(session)
        row = factories.make_campaign(session, user, channels=(LINKEDIN, LINKEDIN), **campaign)
        contact = factories.make_contact(session, user, **_other_contact(9))
        enrollment = factories.make_enrollment(session, row, contact, next_action_at=NOW)
        return row.id, row.steps[0].id, enrollment.id


async def test_the_api_previews_switches_with_a_confirm_and_records_it(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    campaign_id, step_id, enrollment_id = _seed_api(running_app)
    base = f"/api/v1/campaigns/{campaign_id}/steps/{step_id}/mode"

    shown = (await client.get(base)).json()
    assert shown["mode"] == "prefill" and shown["queued"] == 1 and shown["refusal"] is None
    assert shown["auto_send_on"] is False  # the test app's config leaves it off
    assert shown["confirm_text"] == step_mode.AUTO_SEND_CONFIRM

    unconfirmed = await client.put(base, json={"mode": "auto_send"}, headers=CSRF)
    assert unconfirmed.status_code == 422
    assert "confirm" in unconfirmed.json()["detail"]

    done = await client.put(base, json={"mode": "auto_send", "confirm": True}, headers=CSRF)
    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["from_mode"], body["to_mode"], body["changed"]) == ("prefill", "auto_send", True)
    assert body["kept_prefill"] == 1 and body["auto_send_on"] is False
    step = body["campaign"]["steps"][0]
    assert step["mode"] == "auto_send" and step["kept_prefill"] == 1
    assert step["budget"] == "li_prefills"  # auto-send is off: nothing sends on its own

    back = await client.put(base, json={"mode": "prefill"}, headers=CSRF)
    assert back.status_code == 200 and back.json()["to_mode"] == "prefill"

    history = (await client.get(f"/api/v1/campaigns/{campaign_id}/mode-changes")).json()
    assert [(h["from_mode"], h["to_mode"]) for h in history] == [
        ("auto_send", "prefill"),
        ("prefill", "auto_send"),
    ]
    assert set(history[0]) == {
        "id",
        "step_id",
        "position",
        "from_mode",
        "to_mode",
        "changed_at",
        "changed_by",
        "kept_prefill",
        "auto_send_on",
    }
    with session_scope(running_app.state.session_factory) as session:
        messages = session.scalars(scoped(_local(session), Message)).all()
        assert messages == []  # the switch sent nothing
        enrollment = get_scoped(session, _local(session), Enrollment, enrollment_id)
        assert enrollment is not None and enrollment.next_action_at == NOW


async def test_the_api_says_when_auto_send_is_on(
    running_app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = running_app.state.settings
    on = replace(settings, campaigns=replace(settings.campaigns, linkedin_auto_send=True))
    monkeypatch.setattr(running_app.state, "settings", on)
    campaign_id, step_id, _ = _seed_api(running_app)
    base = f"/api/v1/campaigns/{campaign_id}/steps/{step_id}/mode"
    assert (await client.get(base)).json()["auto_send_on"] is True
    done = await client.put(base, json={"mode": "auto_send", "confirm": True}, headers=CSRF)
    assert done.json()["auto_send_on"] is True
    assert done.json()["campaign"]["steps"][0]["budget"] == "li_messages_auto"


async def test_the_api_refuses_a_draft_and_an_email_step(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    campaign_id, step_id, _ = _seed_api(running_app, status=CampaignStatus.DRAFT)
    refused = await client.put(
        f"/api/v1/campaigns/{campaign_id}/steps/{step_id}/mode",
        json={"mode": "auto_send", "confirm": True},
        headers=CSRF,
    )
    assert refused.status_code == 409
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        email = factories.make_campaign(session, _local(session))
        email_id, email_step = email.id, email.steps[0].id
    invalid = await client.put(
        f"/api/v1/campaigns/{email_id}/steps/{email_step}/mode",
        json={"mode": "prefill"},
        headers=CSRF,
    )
    assert invalid.status_code == 422


async def test_the_api_answers_404_for_another_users_campaign(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    campaign_id, step_id, enrollment_id = _seed_api(running_app, owner="other")
    base = f"/api/v1/campaigns/{campaign_id}/steps/{step_id}/mode"
    assert (await client.get(base)).status_code == 404
    put = await client.put(base, json={"mode": "auto_send", "confirm": True}, headers=CSRF)
    assert put.status_code == 404
    listed = await client.get(f"/api/v1/campaigns/{campaign_id}/mode-changes")
    assert listed.status_code == 404
    with session_scope(running_app.state.session_factory) as session:
        step = session.scalars(unscoped(select(CampaignStep).where(CampaignStep.id == step_id)))
        assert step.one().mode is StepMode.PREFILL
        assert enrollment_id > 0


# --- the CLI --------------------------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEP_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    yield factory
    engine.dispose()


def test_the_cli_asks_before_auto_send_and_not_before_prefill(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user, channels=(LINKEDIN,))
        contact = factories.make_contact(session, user, **_other_contact(7))
        factories.make_enrollment(session, campaign, contact, next_action_at=NOW)
        later = factories.make_contact(session, user, **_other_contact(8))
        factories.make_enrollment(
            session, campaign, later, next_action_at=datetime.now(UTC) + timedelta(days=30)
        )
        campaign_id, step_id = campaign.id, campaign.steps[0].id
    runner = CliRunner()

    def mode() -> StepMode:
        with session_scope(cli_db) as session:
            step = get_scoped(session, _local(session), CampaignStep, step_id)
            assert step is not None
            return step.mode

    args = ["campaigns", "step-mode", str(campaign_id), "1"]
    declined = runner.invoke(cli, [*args, "auto_send"], input="n\n")
    assert declined.exit_code == 1
    assert step_mode.AUTO_SEND_CONFIRM in declined.output
    assert "1 contact already queued for the step keeps prefill" in declined.output
    assert "1 contact already scheduled for the step will be sent on its own" in (declined.output)
    assert "auto-send is off" in declined.output
    assert "cancelled: step 1 is still prefill" in declined.output
    assert mode() is StepMode.PREFILL

    accepted = runner.invoke(cli, [*args, "auto_send"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "is now auto_send (was prefill); 1 queued contact keeps prefill" in accepted.output
    assert mode() is StepMode.AUTO_SEND

    back = runner.invoke(cli, [*args, "prefill"])  # no input: it does not ask
    assert back.exit_code == 0, back.output
    assert "is now prefill (was auto_send)" in back.output

    same = runner.invoke(cli, [*args, "prefill"])
    assert same.exit_code == 0 and "already prefill" in same.output

    email_mode = runner.invoke(cli, [*args, "send"])
    assert email_mode.exit_code == 1 and "prefill or auto_send" in email_mode.output
    with session_scope(cli_db) as session:
        assert len(session.scalars(scoped(_local(session), StepModeChange)).all()) == 2
