"""netkeep.services.message_id_check (#510): what the Message-ID check searches for, and
where a mailbox is on the way to arming to send. Read-only; no Gmail."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import factories
from campaign_fakes import NOW, make_mailbox
from sqlalchemy.orm import Session

from netkeep.campaigns.compose import message_id_candidates, message_id_for
from netkeep.models import (
    Campaign,
    CampaignStatus,
    Mailbox,
    MailboxStatus,
    Message,
    MessageStatus,
    TestSend,
    User,
)
from netkeep.scoping import get_scoped
from netkeep.services import message_id_check as check
from netkeep.services.message_id_check import ArmNextStep, MessageIdWait

ARMED: dict[str, Any] = {"armed_at": NOW - timedelta(days=1), "armed_by": "test"}


def _setup(session: Session, **mailbox: Any) -> tuple[User, Mailbox, Campaign]:
    user = factories.make_user(session)
    row = make_mailbox(session, user, **{**ARMED, **mailbox})
    campaign = factories.make_campaign(session, user, mailbox_id=row.id)
    return user, row, campaign


def _message(session: Session, user: User, campaign: Campaign, **fields: Any) -> int:
    contact = factories.make_contact(session, user)
    enrollment = factories.make_enrollment(session, campaign, contact)
    return factories.make_message(session, enrollment, **fields).id


def _test_draft(session: Session, user: User, campaign: Campaign, mailbox: Mailbox) -> TestSend:
    row = TestSend(
        user_id=user.id,
        campaign_id=campaign.id,
        step_id=campaign.steps[0].id,
        fingerprint="f" * 64,
        to_address=mailbox.email,
        gmail_draft_id="draft-test",
        rfc822_message_id="<test@example.test>",
        sent_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def _steps(session: Session, user: User, mailbox: Mailbox) -> check.ArmSteps:
    session.refresh(mailbox)
    return check.arm_steps(session, user, mailbox, now=NOW)


def test_a_draft_seen_sent_is_searched_for_by_netkeeps_own_message_id(session: Session) -> None:
    """Each Message-ID the row may carry, the current seed's first: a draft made before
    the rename carries the old seed's (#721)."""
    user, mailbox, campaign = _setup(session)
    message_id = _message(
        session, user, campaign, sent_at=NOW - timedelta(hours=1), gmail_draft_id="d1"
    )
    [found] = check.sent_drafts_to_verify(session, user, now=NOW)
    stored = get_scoped(session, user, Message, message_id)
    assert stored is not None
    fields: dict[str, Any] = {
        "user_id": user.id,
        "message_id": message_id,
        "created_at": stored.created_at,
        "address": mailbox.email,
    }
    assert found == check.SentDraftCheck(mailbox.id, message_id, message_id_candidates(**fields))
    assert len(found.rfc822_message_ids) == 2
    assert found.rfc822_message_ids[0] == message_id_for(**fields)


def test_only_recent_sent_drafts_on_an_armed_unverified_mailbox_count(session: Session) -> None:
    user, mailbox, campaign = _setup(session)
    # Sent by messages.send (no draft), too old, still a draft, or newest first beyond three.
    _message(session, user, campaign, sent_at=NOW - timedelta(hours=1))
    _message(session, user, campaign, sent_at=NOW - timedelta(days=15), gmail_draft_id="old")
    _message(session, user, campaign, status=MessageStatus.DRAFTED, gmail_draft_id="waiting")
    recent = [
        _message(session, user, campaign, sent_at=NOW - timedelta(hours=h), gmail_draft_id=f"d{h}")
        for h in (5, 4, 3, 2)
    ]
    found = check.sent_drafts_to_verify(session, user, now=NOW)
    assert [c.message_id for c in found] == list(reversed(recent))[:3]

    mailbox.armed_at = None
    session.flush()
    assert check.sent_drafts_to_verify(session, user, now=NOW) == []
    mailbox.armed_at = NOW
    mailbox.message_id_verified_at = NOW
    session.flush()
    assert check.sent_drafts_to_verify(session, user, now=NOW) == []


def test_each_reason_the_check_waits_and_the_button_for_it(session: Session) -> None:
    user, mailbox, campaign = _setup(session, armed_at=None)
    steps = _steps(session, user, mailbox)
    assert (steps.waiting, steps.next_step, steps.can_arm_send) == (
        MessageIdWait.NOT_ARMED,
        ArmNextStep.ARM_DRAFTS,
        False,
    )
    mailbox.armed_at = NOW
    session.flush()
    assert _steps(session, user, mailbox).waiting is MessageIdWait.NO_DRAFT
    assert _steps(session, user, mailbox).next_step is ArmNextStep.SEND_TEST

    test = _test_draft(session, user, campaign, mailbox)
    assert _steps(session, user, mailbox).waiting is MessageIdWait.NOT_CHECKED
    assert _steps(session, user, mailbox).next_step is ArmNextStep.CHECK_NOW
    test.not_found_at = NOW
    session.flush()
    assert _steps(session, user, mailbox).waiting is MessageIdWait.TEST_DRAFT_NOT_FOUND
    assert _steps(session, user, mailbox).next_step is ArmNextStep.SEND_TEST

    _message(session, user, campaign, sent_at=NOW - timedelta(hours=1), gmail_draft_id="d1")
    assert _steps(session, user, mailbox).waiting is MessageIdWait.NOT_CHECKED
    check.record_checked(session, user, mailbox.id, now=NOW)
    steps = _steps(session, user, mailbox)
    assert (steps.waiting, steps.checked_at, steps.next_step) == (
        MessageIdWait.NOT_FOUND,
        NOW,
        ArmNextStep.CHECK_NOW,
    )

    mailbox.status = MailboxStatus.REAUTH_REQUIRED
    session.flush()
    steps = _steps(session, user, mailbox)
    assert (steps.waiting, steps.next_step) == (
        MessageIdWait.REAUTH_REQUIRED,
        ArmNextStep.REAUTHORIZE,
    )

    mailbox.status = MailboxStatus.DISABLED
    session.flush()
    steps = _steps(session, user, mailbox)
    assert (steps.waiting, steps.next_step) == (MessageIdWait.DISCONNECTED, None)


def test_arming_to_send_is_ready_only_once_verified_and_armed_for_drafts(
    session: Session,
) -> None:
    user, mailbox, _ = _setup(session, armed_at=None, message_id_verified_at=NOW)
    steps = _steps(session, user, mailbox)
    assert (steps.waiting, steps.can_arm_send, steps.next_step) == (
        None,
        False,
        ArmNextStep.ARM_DRAFTS,
    )
    mailbox.armed_at = NOW
    session.flush()
    steps = _steps(session, user, mailbox)
    assert (steps.can_arm_send, steps.next_step) == (True, ArmNextStep.ARM_SEND)
    mailbox.send_armed_at = NOW
    session.flush()
    steps = _steps(session, user, mailbox)
    assert (steps.armed_to_send, steps.can_arm_send, steps.next_step) == (True, False, None)


def test_the_send_a_test_button_names_a_campaign_in_review(session: Session) -> None:
    user, mailbox, _ = _setup(session)  # the factory's campaign is active
    assert _steps(session, user, mailbox).test_campaign_id is None
    review = factories.make_campaign(
        session, user, mailbox_id=mailbox.id, status=CampaignStatus.REVIEWING
    )
    assert _steps(session, user, mailbox).test_campaign_id == review.id


def test_describe_says_each_step_for_the_cli(session: Session) -> None:
    user, mailbox, campaign = _setup(session)
    assert check.describe(_steps(session, user, mailbox)) == [
        "1. armed for drafts: done",
        "2. netkeep found its own draft: waiting: no draft yet; send a test from a"
        " campaign's review",
        "3. arm to send: not yet: waits for the steps above",
    ]
    _message(session, user, campaign, sent_at=NOW - timedelta(hours=1), gmail_draft_id="d1")
    check.record_checked(session, user, mailbox.id, now=NOW)
    assert check.describe(_steps(session, user, mailbox))[1] == (
        "2. netkeep found its own draft: waiting: not found yet; last checked 2026-09-29 14:00 UTC"
    )
    mailbox.message_id_verified_at = NOW
    session.flush()
    assert check.describe(_steps(session, user, mailbox))[1:] == [
        "2. netkeep found its own draft: done 2026-09-29 14:00 UTC",
        "3. arm to send: ready: `netkeep gmail arm --send`",
    ]
