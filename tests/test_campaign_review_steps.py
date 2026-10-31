"""One approval per step, through the engine (spec 11.8; #339).

A step approval is given once and covers the step's messages, but a message that
is blocked when the step is reviewed (it renders with no subject, or a guard
excludes the contact) is never covered by it, and never sends once the campaign
is active.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

import factories
import pytest
from campaign_fakes import ARMED_FOR_SEND, NOW, SETTINGS, FakeSender, make_mailbox
from sqlalchemy.orm import Session, sessionmaker

from netkeep.campaigns import templates as template_service
from netkeep.campaigns.render import LONGEST_AGO_DATE, LintIssue, LintRule, Severity, ago, lint
from netkeep.campaigns.templates import activation_errors
from netkeep.crm import do_not_send
from netkeep.crm import lists as list_service
from netkeep.crm.self_contact import update_self_contact
from netkeep.db import session_scope
from netkeep.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    ListKind,
    Message,
    MessageStatus,
    StepMode,
    Template,
    TemplateChannel,
    User,
)
from netkeep.scoping import get_scoped, scoped
from netkeep.services import campaign_engine, campaign_review
from netkeep.services.campaign_engine import Decision, Skip, run_tick
from netkeep.services.campaign_guards import Reason, check_step


@dataclass
class Reviewed:
    factory: sessionmaker[Session]
    user: User
    campaign_id: int
    step_id: int
    ok: list[int]
    no_subject: int
    excluded: int


def _reviewing(
    factory: sessionmaker[Session], *, subject: str, body: str, bob_elsewhere: bool = False
) -> Reviewed:
    """A one-step email campaign under review: two contacts whose message can be sent,
    one whose subject renders empty, and one a guard excludes. ``bob_elsewhere``: bob
    is first enrolled in a draft campaign, an enrollment older than this one's."""
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
        campaign = factories.make_campaign(
            session,
            user,
            channels=(TemplateChannel.EMAIL,),
            status=CampaignStatus.REVIEWING,
            mailbox_id=mailbox.id,
        )
        step = campaign.steps[0]
        step.mode = StepMode.SEND
        template = get_scoped(session, user, Template, step.template_id)
        assert template is not None
        template.subject, template.body = subject, body

        other = factories.make_campaign(session, user, status=CampaignStatus.DRAFT)

        def enroll(email: str, **contact: Any) -> int:
            row = factories.make_contact(session, user, emails=[email], **contact)
            if bob_elsewhere and email.startswith("bob@"):
                factories.make_enrollment(session, other, row, status=EnrollmentStatus.PENDING)
            return factories.make_enrollment(
                session, campaign, row, status=EnrollmentStatus.PENDING
            ).id

        ok = [enroll("ada@contacts.example"), enroll("bob@contacts.example")]
        no_subject = enroll("nameless@contacts.example", first_name="", preferred_name=None)
        excluded = enroll("dnc@contacts.example", do_not_contact=True)
        return Reviewed(factory, user, campaign.id, step.id, ok, no_subject, excluded)


def _review(r: Reviewed) -> campaign_review.StepReview:
    with session_scope(r.factory) as session:
        return campaign_review.review_step(session, r.user, r.campaign_id, r.step_id, now=NOW)


def _complete_and_activate(r: Reviewed, review: campaign_review.StepReview) -> None:
    with session_scope(r.factory, write=True) as session:
        user = r.user
        campaign_review.approve_step(
            session,
            user,
            r.campaign_id,
            r.step_id,
            fingerprint_seen=review.fingerprint,
            now=NOW,
        )
        update_self_contact(session, user, {"first_name": "Selfie"})  # the test renders it
        plan = campaign_review.prepare_test_send(
            session, user, r.campaign_id, r.step_id, today=NOW.date()
        )
        campaign_review.record_test_send(session, user, plan, gmail_message_id="fake", now=NOW)
        assert campaign_review.record_lint(session, user, r.campaign_id, now=NOW).clean
        campaign_review.activate(
            session, user, r.campaign_id, settings=SETTINGS, now=NOW, starts_at=NOW
        )


def _tick_until_quiet(r: Reviewed, sender: FakeSender) -> list[Decision]:
    """Tick until nothing more fires; every decision the ticks made."""
    decisions: list[Decision] = []
    for n in range(12):
        at = NOW + timedelta(minutes=1 + 10 * n)
        sender.now = at
        for result in run_tick(
            r.factory,
            settings=SETTINGS,
            sender=sender,
            clock=partial(_at, at),
            rng=random.Random(n),
        ):
            decisions.extend(result.decisions)
    return decisions


def _at(when: datetime) -> datetime:
    return when


def test_blocked_messages_are_listed_apart_and_never_sent(
    session_factory: sessionmaker[Session],
) -> None:
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")
    review = _review(r)
    assert [m.enrollment_id for m in review.messages] == r.ok
    blocked = {m.enrollment_id: m.blocked for m in review.blocked}
    assert blocked == {
        r.no_subject: "renders with no subject",
        r.excluded: "excluded by a guard: do-not-contact",
    }
    _complete_and_activate(r, review)
    assert all(m.approved for m in _review(r).messages)
    assert not any(m.approved for m in _review(r).blocked)

    sender = FakeSender()
    _tick_until_quiet(r, sender)

    sent_to = sorted(f.to_address or "" for f in sender.firings)
    assert sent_to == ["ada@contacts.example", "bob@contacts.example"]
    with session_scope(r.factory) as session:
        for enrollment_id in (r.no_subject, r.excluded):
            assert not list(
                session.scalars(
                    scoped(r.user, Message).where(
                        Message.enrollment_id == enrollment_id,
                        Message.status == MessageStatus.SENT,
                    )
                )
            )
            enrollment = get_scoped(session, r.user, Enrollment, enrollment_id)
            assert enrollment is not None and enrollment.current_step is None


def _exclude_bob(
    session: Session, user: User, campaign_id: int, contact: Contact, how: Reason
) -> None:
    """Make a guard exclude bob, after activation, for ``how``."""
    if how is Reason.DO_NOT_CONTACT:
        contact.do_not_contact = True
    elif how is Reason.NO_EMAIL:
        contact.emails.clear()
    elif how is Reason.EMAIL_INVALID:
        contact.emails[0].status = EmailStatus.INVALID
    elif how is Reason.NEEDS_REVIEW:
        contact.needs_review_at = NOW
    elif how is Reason.ARCHIVED:
        contact.archived_at = NOW
    elif how is Reason.MERGED:
        contact.merged_into_id = factories.make_contact(session, user).id
    elif how is Reason.DO_NOT_SEND:
        do_not_send.add_by_hand(session, user, "bob@contacts.example")
    elif how is Reason.ADDRESS_BOUNCED_ELSEWHERE:
        twin = factories.make_contact(session, user, emails=["bob@contacts.example"])
        twin.emails[0].status = EmailStatus.BOUNCED
    elif how is Reason.IN_ANOTHER_CAMPAIGN:
        # Bob's older enrollment, in a campaign that was a draft, now runs.
        for campaign in session.scalars(scoped(user, Campaign).where(Campaign.id != campaign_id)):
            campaign.status = CampaignStatus.REVIEWING
    else:
        raise AssertionError(how)
    session.flush()


@pytest.mark.parametrize(
    "how",
    [
        Reason.DO_NOT_CONTACT,
        Reason.NO_EMAIL,
        Reason.EMAIL_INVALID,
        Reason.NEEDS_REVIEW,
        Reason.DO_NOT_SEND,
        Reason.IN_ANOTHER_CAMPAIGN,
        Reason.ARCHIVED,
        Reason.MERGED,
        Reason.ADDRESS_BOUNCED_ELSEWHERE,
    ],
    ids=lambda r: r.value,
)
def test_a_contact_a_guard_excludes_after_activation_is_never_sent(
    session_factory: sessionmaker[Session], how: Reason
) -> None:
    """#346: activation needs no guard acknowledgement, and the guards still apply when
    the step fires: a contact a guard excludes only after the review, the approval and
    activation is sent nothing, whatever the guard. The campaign is one email step, so
    each address guard is the one deciding, and the tick's decision names it."""
    r = _reviewing(
        session_factory,
        subject="{{ first_name }}",
        body="Hi {{ first_name }}",
        bob_elsewhere=how is Reason.IN_ANOTHER_CAMPAIGN,
    )
    with session_scope(r.factory) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        assert campaign_review.guard_summary(session, r.user, campaign, now=NOW) == (
            "3 will start, 1 skipped (1 do-not-contact)"
        )
    _complete_and_activate(r, _review(r))
    with session_scope(r.factory, write=True) as session:
        enrollment = get_scoped(session, r.user, Enrollment, r.ok[1])
        assert enrollment is not None
        contact = get_scoped(session, r.user, Contact, enrollment.contact_id)
        assert contact is not None
        _exclude_bob(session, r.user, r.campaign_id, contact, how)
        step = get_scoped(session, r.user, CampaignStep, r.step_id)
        assert step is not None
        assert step.channel is TemplateChannel.EMAIL
        assert check_step(session, r.user, enrollment, step, now=NOW).reasons == (how,)

    sender = FakeSender()
    decisions = _tick_until_quiet(r, sender)

    skipped = [d for d in decisions if d.enrollment_id == r.ok[1]]
    assert skipped and all(not d.fired for d in skipped)
    # Other decisions only wait for the mailbox's spacing; the guard's names its reason,
    # and that reason alone.
    [guarded] = [d.reasons for d in skipped if d.reasons != (Skip.SPACING,)]
    assert guarded in {(Skip.GUARD_EXCLUDED, how.value), (Skip.ENDED, how.value)}

    assert sorted(f.to_address or "" for f in sender.firings) == ["ada@contacts.example"]
    with session_scope(r.factory) as session:
        assert not list(
            session.scalars(
                scoped(r.user, Message).where(
                    Message.enrollment_id == r.ok[1], Message.status == MessageStatus.SENT
                )
            )
        )


def test_a_contact_pending_in_two_campaigns_starts_only_in_the_older_enrollment(
    session_factory: sessionmaker[Session],
) -> None:
    """#346 review: another campaign counts only for an older enrollment, in the summary
    as in check_step. The older campaign counts the contact as starting; the newer one
    skips it as in another campaign."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
        x = factories.make_contact(session, user, emails=["x@contacts.example"])
        older, newer = (
            factories.make_campaign(
                session,
                user,
                channels=(TemplateChannel.EMAIL,),
                status=CampaignStatus.REVIEWING,
                mailbox_id=mailbox.id,
            )
            for _ in range(2)
        )
        in_older = factories.make_enrollment(session, older, x, status=EnrollmentStatus.PENDING)
        in_newer = factories.make_enrollment(session, newer, x, status=EnrollmentStatus.PENDING)
        assert in_older.id < in_newer.id

        def report(campaign: Campaign) -> campaign_review.GuardReport:
            return campaign_review.guard_report(session, user, campaign, now=NOW)

        def at_fire(enrollment: Enrollment, campaign: Campaign) -> set[Reason]:
            verdict = check_step(session, user, enrollment, campaign.steps[0], now=NOW)
            # Pending and reviewing: what check_step adds for not being live yet.
            return set(verdict.reasons) - {
                Reason.ENROLLMENT_NOT_ACTIVE,
                Reason.CAMPAIGN_NOT_ACTIVE,
            }

        first, second = report(older), report(newer)
        assert (first.summary, first.will_start, first.skipped) == (
            "1 will start, none skipped",
            1,
            (),
        )
        assert at_fire(in_older, older) == set()
        assert second.summary == "0 will start, 1 skipped (1 in another campaign)"
        assert [(c.contact_id, c.reasons) for c in second.skipped] == [
            (x.id, ("in another campaign",))
        ]
        assert at_fire(in_newer, newer) == {Reason.IN_ANOTHER_CAMPAIGN}


def _summary(r: Reviewed) -> campaign_review.GuardReport:
    with session_scope(r.factory) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        return campaign_review.guard_report(session, r.user, campaign, now=NOW)


def test_of_two_pending_enrollments_sharing_an_address_only_the_newer_is_skipped(
    session_factory: sessionmaker[Session],
) -> None:
    """#346 review: the summary judges a pending enrollment as the engine judges its
    first step, so it counts the older of two sharing an address as starting, and the
    engine sends to it, once."""
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")
    with session_scope(r.factory, write=True) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        twin = factories.make_contact(session, r.user, emails=["ada@contacts.example"])
        newer = factories.make_enrollment(
            session, campaign, twin, status=EnrollmentStatus.PENDING
        ).id

    report = _summary(r)
    assert report.summary == (
        "3 will start, 2 skipped (1 do-not-contact, 1 address already in this campaign)"
    )
    assert [(c.contact_id, c.reasons) for c in report.skipped if c.contact_id == twin.id] == [
        (twin.id, ("address already in this campaign",))
    ]
    review = _review(r)
    blocked = {m.enrollment_id: m.blocked for m in review.blocked}
    assert blocked[newer] == "excluded by a guard: address already in this campaign"
    assert r.ok[0] not in blocked  # the older one sends, in the review as in the engine

    _complete_and_activate(r, review)
    sender = FakeSender()
    _tick_until_quiet(r, sender)

    assert sorted(f.to_address or "" for f in sender.firings) == [
        "ada@contacts.example",
        "bob@contacts.example",
    ]
    with session_scope(r.factory) as session:
        sent = set(
            session.scalars(
                scoped(r.user, Message)
                .with_only_columns(Message.enrollment_id)
                .where(Message.status == MessageStatus.SENT)
            )
        )
    assert sent == set(r.ok)


def test_a_source_contact_not_enrolled_is_judged_against_the_pending_enrollments(
    session_factory: sessionmaker[Session],
) -> None:
    """#346 review: a source contact sharing a pending enrollment's address is skipped,
    as enrolling it would be; the pending one still starts."""
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")
    with session_scope(r.factory, write=True) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        twin = factories.make_contact(session, r.user, emails=["bob@contacts.example"])
        loner = factories.make_contact(session, r.user, emails=["cy@contacts.example"])
        members = [
            *session.scalars(
                scoped(r.user, Enrollment)
                .with_only_columns(Enrollment.contact_id)
                .where(Enrollment.campaign_id == r.campaign_id)
            ),
            twin.id,
            loner.id,
        ]
        source = list_service.create_list(session, r.user, "Source", ListKind.STATIC)
        list_service.add_members(session, r.user, source.id, members)
        campaign.source_list_id = source.id

    report = _summary(r)
    assert report.summary == (
        "3 will start, 2 skipped (1 do-not-contact, 1 address already in this campaign),"
        " 1 not enrolled"
    )
    assert twin.id in {c.contact_id for c in report.skipped}
    with session_scope(r.factory, write=True) as session:
        result = campaign_engine.enroll(
            session, r.user, r.campaign_id, [twin.id, loner.id], now=NOW
        )
        assert [v.contact_id for v in result.verdicts if v.eligible] == [loner.id]
        assert {v.contact_id: v.reasons for v in result.verdicts}[twin.id] == (
            Reason.DUPLICATE_ADDRESS,
        )


def test_a_lint_error_blocks_every_message_and_activation(
    session_factory: sessionmaker[Session],
) -> None:
    """A template with a lint error blocks each message and the gate; approving the step
    does not cover them."""
    r = _reviewing(session_factory, subject="Hello", body="Hi {{ nope }} {{ first_name }}")
    review = _review(r)
    assert review.total == 0
    assert {m.enrollment_id for m in review.blocked} == {*r.ok, r.no_subject, r.excluded}
    assert all(
        m.blocked is not None and m.blocked.startswith("lint error:")
        for m in review.blocked
        if m.enrollment_id != r.excluded
    )
    with session_scope(r.factory, write=True) as session:
        campaign_review.approve_step(
            session,
            r.user,
            r.campaign_id,
            r.step_id,
            fingerprint_seen=review.fingerprint,
            now=NOW,
        )
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        gaps = campaign_review.missing(session, r.user, campaign, now=NOW)
        assert "lint" in {g.requirement for g in gaps}
        with pytest.raises(campaign_review.ReviewIncomplete):
            campaign_review.activate(
                session, r.user, r.campaign_id, settings=SETTINGS, now=NOW, starts_at=NOW
            )


def test_the_summary_alone_never_reads_the_old_tools_history(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")

    def unwanted(*_: object) -> None:
        pytest.fail("the note was read for a caller that does not show it")

    monkeypatch.setattr(campaign_review, "prior_contact", unwanted)
    with session_scope(r.factory) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        assert campaign_review.guard_summary(session, r.user, campaign, now=NOW) == (
            "3 will start, 1 skipped (1 do-not-contact)"
        )
        assert campaign_review.guard_summary_and_note(
            session, r.user, campaign, now=NOW, include_note=False
        ) == ("3 will start, 1 skipped (1 do-not-contact)", None)


def _linkedin_reviewing(
    factory: sessionmaker[Session],
    *,
    subject: str | None,
    body: str,
    locations: tuple[str, ...] = ("Lisbon",),
) -> Any:
    """A one-step LinkedIn campaign under review, with a contact enrolled for each of
    ``locations``."""
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        campaign = factories.make_campaign(
            session, user, channels=(TemplateChannel.LINKEDIN,), status=CampaignStatus.REVIEWING
        )
        template = get_scoped(session, user, Template, campaign.steps[0].template_id)
        assert template is not None
        template.subject, template.body = subject, body
        for location in locations:
            contact = factories.make_contact(session, user, location=location)
            factories.make_enrollment(session, campaign, contact, status=EnrollmentStatus.PENDING)
        return user, campaign.id


@pytest.fixture
def newlines_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The newline flag as it was before P4-03 (#382), and as it goes back to if a later
    capture shows Shift+Enter sending: the tests that use it pin the refusing branch."""
    from netkeep.campaigns import render as render_module

    monkeypatch.setattr(render_module, "SHIFT_ENTER_NEWLINES_ALLOWED", False)


@pytest.mark.parametrize(
    ("subject", "body", "rule"),
    [
        (None, "Hi {{ first_name }},\nthanks", LintRule.LINKEDIN_NEWLINE),
        (None, "Hi {{ first_name }} " + "x" * 8000, LintRule.LINKEDIN_TOO_LONG),
    ],
    ids=["newline", "too_long"],
)
@pytest.mark.usefixtures("newlines_refused")
def test_a_linkedin_step_that_fails_lint_blocks_activation(
    session_factory: sessionmaker[Session], subject: str | None, body: str, rule: LintRule
) -> None:
    """P4-11: what the prefill would refuse fails lint, and activation needs a clean lint."""
    user, campaign_id = _linkedin_reviewing(session_factory, subject=subject, body=body)
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert not result.clean
        [(_position, issues)] = result.steps
        assert [issue.rule for issue in issues] == [rule]
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        gaps = campaign_review.missing(session, user, campaign, now=NOW)
        assert "lint" in {g.requirement for g in gaps}
        with pytest.raises(campaign_review.ReviewIncomplete):
            campaign_review.activate(
                session, user, campaign_id, settings=SETTINGS, now=NOW, starts_at=NOW
            )


def test_a_long_linkedin_message_is_a_warning_that_does_not_block_lint(
    session_factory: sessionmaker[Session],
) -> None:
    user, campaign_id = _linkedin_reviewing(
        session_factory, subject=None, body="Hi {{ first_name }} " + "x" * 1100
    )
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert result.clean
        assert result.steps == ((1, ()),)


def test_spelling_warnings_show_in_the_review_and_never_block_activation(
    session_factory: sessionmaker[Session],
) -> None:
    """#522: a misspelling is a warning the lint review returns; the lint is still clean
    and recorded, so activation is not held for it."""
    user, campaign_id = _linkedin_reviewing(
        session_factory, subject=None, body="Hi {{ first_name }}, I hope you recieve this"
    )
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert result.clean
        assert result.steps == ((1, ()),)
        [(position, warnings)] = result.warnings
        assert position == 1
        assert [(i.rule, i.severity, i.field) for i in warnings] == [
            (LintRule.SPELLING, Severity.WARNING, "recieve")
        ]
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        gaps = campaign_review.missing(session, user, campaign, now=NOW)
        assert "lint" not in {g.requirement for g in gaps}


def test_a_failing_spelling_check_still_records_the_lint(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#535 review: spelling fails soft, so an error in it never keeps a clean lint from
    being recorded, and only the error's type is logged."""

    def broken(*_args: object, **_kwargs: object) -> list[LintIssue]:
        raise RuntimeError("Glimmerwick")

    monkeypatch.setattr(template_service, "spelling_issues", broken)
    user, campaign_id = _linkedin_reviewing(
        session_factory, subject=None, body="Hi {{ first_name }}, I hope you recieve this"
    )
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert result.clean
        assert result.warnings == ((1, ()),)
    with session_scope(session_factory) as session:
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        assert campaign.lint_checked_at is not None
        gaps = campaign_review.missing(session, user, campaign, now=NOW)
        assert "lint" not in {g.requirement for g in gaps}
    assert "RuntimeError" in caplog.text and "Glimmerwick" not in caplog.text


def test_a_legacy_linkedin_subject_is_a_warning_that_does_not_block_activation(
    session_factory: sessionmaker[Session],
) -> None:
    """#448: a LinkedIn template saved with a subject before the rule shows the warning, and
    neither lint nor activation refuses it (even a removed ``me.*`` field inside it)."""
    user, campaign_id = _linkedin_reviewing(
        session_factory, subject="Hi {{ me.first_name }}", body="Hi {{ first_name }}"
    )
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert result.clean
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        template = campaign.steps[0].template
        assert activation_errors(template) == []
        assert [i.rule for i in lint(template.channel, template.subject, template.body)] == [
            LintRule.LINKEDIN_SUBJECT
        ]
        gaps = campaign_review.missing(session, user, campaign, now=NOW)
        assert "lint" not in {g.requirement for g in gaps}


@pytest.mark.parametrize(
    ("timezone", "now", "connected_on", "refused"),
    [
        # Mon 16 Nov 16:10 PST is 00:10 UTC on the 17th: the UTC day reached the second
        # anniversary a day early, so a clean message was refused.
        (
            "America/Los_Angeles",
            datetime(2026, 11, 17, 0, 10, tzinfo=UTC),
            date(2024, 11, 17),
            False,
        ),
        # Mon 16 Nov 09:30 AEST is 23:30 UTC on the 15th: the UTC day had not reached the
        # anniversary yet, so a message the engine would refuse was approved.
        (
            "Australia/Brisbane",
            datetime(2026, 11, 15, 23, 30, tzinfo=UTC),
            date(2024, 11, 16),
            True,
        ),
    ],
    ids=["us-pacific-winter-evening", "aest-morning"],
)
@pytest.mark.usefixtures("newlines_refused")
def test_approving_a_message_lints_it_on_the_users_local_day(
    session_factory: sessionmaker[Session],
    timezone: str,
    now: datetime,
    connected_on: date,
    refused: bool,
) -> None:
    """#709 (I10): the approval's lint renders with the user's local today, as the review
    and the engine do. Two years connected adds a line the prefill would refuse."""
    user, campaign_id = _linkedin_reviewing(
        session_factory,
        subject=None,
        body="Hi {{ first_name }} {{ personal_line }}"
        "{% if years_since_connected >= 2 %} in {{ location }}{% endif %}",
        locations=("Two\nlines",),
    )
    user.timezone = timezone  # the caller's loaded user, as each request loads it
    with session_scope(session_factory, write=True) as session:
        row = session.get(User, user.id)
        assert row is not None
        row.timezone = timezone
        for contact in session.scalars(scoped(user, Contact)):
            contact.connected_on = connected_on
    with session_scope(session_factory) as session:
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        step_id = campaign.steps[0].id
        review = campaign_review.review_step(session, user, campaign_id, step_id, now=now)
    [message] = review.messages  # a per-message step pages through blocked ones too
    assert (message.blocked is not None) is refused
    approve = partial(
        campaign_review.approve_messages,
        user=user,
        campaign_id=campaign_id,
        step_id=step_id,
        seen={message.enrollment_id: message.fingerprint},
        now=now,
    )
    with session_scope(session_factory, write=True) as session:
        if refused:
            with pytest.raises(campaign_review.ReviewConflict, match="lint error"):
                approve(session)
        else:
            assert [row.enrollment_id for row in approve(session)] == [message.enrollment_id]


@pytest.mark.usefixtures("newlines_refused")
def test_a_message_whose_render_has_a_lint_error_cannot_be_approved(
    session_factory: sessionmaker[Session],
) -> None:
    """P4-11: a merge value that makes a LinkedIn message multi-line blocks it, and a
    per-message approval of it is refused, not stored."""
    user, campaign_id = _linkedin_reviewing(
        session_factory,
        subject=None,
        body="Hi {{ first_name }} {{ personal_line }} in {{ location }}",
        locations=("Lisbon", "Two\nlines"),
    )
    with session_scope(session_factory) as session:
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        step_id = campaign.steps[0].id
        review = campaign_review.review_step(session, user, campaign_id, step_id, now=NOW)
    every = {m.enrollment_id: m for m in (*review.messages, *review.blocked)}
    bad = [m for m in every.values() if m.blocked is not None]
    good = [m for m in every.values() if m.blocked is None]
    assert len(bad) == 1 and len(good) == 1
    assert bad[0].blocked == (
        "lint error: LinkedIn messages must be one paragraph: the prefill never presses Enter"
    )
    refused = pytest.raises(campaign_review.ReviewConflict, match="can't be approved: lint error")
    with session_scope(session_factory, write=True) as session, refused:
        campaign_review.approve_messages(
            session,
            user,
            campaign_id,
            step_id,
            {bad[0].enrollment_id: bad[0].fingerprint},
            now=NOW,
        )
    with session_scope(session_factory, write=True) as session:
        approved = campaign_review.approve_messages(
            session,
            user,
            campaign_id,
            step_id,
            {good[0].enrollment_id: good[0].fingerprint},
            now=NOW,
        )
        assert [row.enrollment_id for row in approved] == [good[0].enrollment_id]


# --- #731 review: the review shows the ago words and lints the longest ones -----------


AGO_BODY = "Hi {{ first_name }}{{ personal_line }}, it has been {{ previous_send_date | ago }}"


def _two_step_linkedin(
    factory: sessionmaker[Session], *, body: str, second_delay: int = 6
) -> tuple[User, int, list[int]]:
    """A two-step LinkedIn campaign under review whose steps both render ``body``, with
    one contact, Ada, enrolled. The second step waits ``second_delay`` days."""
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        campaign = factories.make_campaign(
            session,
            user,
            channels=(TemplateChannel.LINKEDIN, TemplateChannel.LINKEDIN),
            status=CampaignStatus.REVIEWING,
        )
        for step in campaign.steps:
            template = get_scoped(session, user, Template, step.template_id)
            assert template is not None
            template.subject, template.body = None, body
        campaign.steps[1].delay_days = second_delay
        contact = factories.make_contact(session, user, first_name="Ada", connected_on=None)
        factories.make_enrollment(session, campaign, contact, status=EnrollmentStatus.PENDING)
        return user, campaign.id, [step.id for step in campaign.steps]


def _only_message(
    factory: sessionmaker[Session], user: User, campaign_id: int, step_id: int, now: datetime
) -> campaign_review.MessagePreview:
    with session_scope(factory) as session:
        review = campaign_review.review_step(session, user, campaign_id, step_id, now=now)
    # A per-message step pages through its blocked messages too, so one may be in both.
    [message] = {m.enrollment_id: m for m in (*review.messages, *review.blocked)}.values()
    return message


def _approve(
    factory: sessionmaker[Session],
    user: User,
    campaign_id: int,
    step_id: int,
    message: campaign_review.MessagePreview,
    now: datetime = NOW,
) -> None:
    with session_scope(factory, write=True) as session:
        campaign_review.approve_messages(
            session,
            user,
            campaign_id,
            step_id,
            {message.enrollment_id: message.fingerprint},
            now=now,
        )


def test_the_review_shows_the_ago_words_for_the_steps_delay(
    session_factory: sessionmaker[Session],
) -> None:
    """A pending enrollment has sent nothing, so the review stands in the earliest the
    previous send can be: the step's delay before today. The first step has none, as
    at its fire."""
    user, campaign_id, (first, second) = _two_step_linkedin(session_factory, body=AGO_BODY)
    assert _only_message(session_factory, user, campaign_id, first, NOW).body == (
        "Hi Ada, it has been "
    )
    assert _only_message(session_factory, user, campaign_id, second, NOW).body == (
        "Hi Ada, it has been 6 days ago"
    )


def test_a_message_fingerprint_leaves_the_gap_out(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-in is the step's delay, which the step's fingerprint already holds, so a
    message's fingerprint doesn't hash the gap: the same tomorrow, and the same for
    another gap with the step unchanged."""
    user, campaign_id, (_, second) = _two_step_linkedin(session_factory, body=AGO_BODY)
    shown = _only_message(session_factory, user, campaign_id, second, NOW)
    tomorrow = _only_message(session_factory, user, campaign_id, second, NOW + timedelta(days=1))
    assert tomorrow.fingerprint == shown.fingerprint
    monkeypatch.setattr(
        campaign_review,
        "previous_send_for_review",
        lambda step, today: today - timedelta(days=step.delay_days + 1),
    )
    other_gap = _only_message(session_factory, user, campaign_id, second, NOW)
    assert other_gap.body == "Hi Ada, it has been last week"
    assert other_gap.fingerprint == shown.fingerprint


@pytest.mark.parametrize("index", [0, 1], ids=["first step", "later step"])
def test_a_message_fingerprint_is_unchanged_so_no_approval_resets(
    session_factory: sessionmaker[Session], index: int
) -> None:
    """The fingerprint is the digest it was before the ago words were shown, for every
    step, so no per-message approval given before the upgrade is undone."""
    user, campaign_id, step_ids = _two_step_linkedin(session_factory, body=AGO_BODY)
    message = _only_message(session_factory, user, campaign_id, step_ids[index], NOW)
    with session_scope(session_factory) as session:
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        step = campaign.steps[index]
        template = get_scoped(session, user, Template, step.template_id)
        contact = get_scoped(session, user, Contact, message.contact_id)
        assert template is not None and contact is not None
        values = campaign_review.merge_values(
            contact, campaign, step, NOW.date(), enrollment_id=message.enrollment_id
        )
        fields = dict(values.contact)
        fields.pop("preferred_name", None)
        before = campaign_review._digest(
            [
                campaign_review.step_fingerprint(step, template, campaign),
                {
                    "contact": fields,
                    "campaign_name": values.campaign_name,
                    "step_number": values.step_number,
                    "personal_line": values.personal_line,
                },
                campaign_review._address(contact),
            ]
        )
    assert message.fingerprint == before


def test_the_longest_ago_words_that_make_a_message_too_slow_to_type_block_its_approval(
    session_factory: sessionmaker[Session],
) -> None:
    """The review shows the step's delay in words, but lints the longest words ago can
    say: a message that fits with the shown words but not with the longest is blocked,
    and its approval refused."""
    from netkeep.linkedin.pacing import TYPING_LINT_SECONDS, typing_expected_seconds

    longest = ago(LONGEST_AGO_DATE, today=NOW.date())  # four digits of years, any zone
    for size in range(1, 4000):
        filler = ("word " * 800)[:size]
        if typing_expected_seconds(f"Hi Ada, {filler}it has been {longest}") > TYPING_LINT_SECONDS:
            break
    shown = f"Hi Ada, {filler}it has been 6 days ago"
    assert typing_expected_seconds(shown) <= TYPING_LINT_SECONDS  # fits with the shown words
    body = "Hi {{ first_name }}{{ personal_line }}, " + filler + "it has been "
    user, campaign_id, (_, second) = _two_step_linkedin(
        session_factory, body=body + "{{ previous_send_date | ago }}"
    )
    message = _only_message(session_factory, user, campaign_id, second, NOW)
    assert message.body == shown
    assert message.blocked is not None and message.blocked.startswith("lint error:")
    with pytest.raises(campaign_review.ReviewConflict, match="can't be approved: lint error"):
        _approve(session_factory, user, campaign_id, second, message)


def test_a_template_that_branches_on_the_previous_send_is_approved_on_its_sent_branch(
    session_factory: sessionmaker[Session],
) -> None:
    """Before #731's review, every step rendered with no previous send, so a template
    that branches on it was reviewed and linted on its ``else``, and an ``else`` too slow
    to type refused the approval. A later step now renders the branch it sends, so its
    approval succeeds; the first step still has no previous send, as at its fire, and
    is still refused."""
    from netkeep.linkedin.pacing import TYPING_LINT_SECONDS, typing_expected_seconds

    words = 1
    while typing_expected_seconds("Hi Ada, " + "word " * words) <= TYPING_LINT_SECONDS:
        words += 1
    body = (
        "Hi {{ first_name }}{{ personal_line }}, {% if previous_send_date %}good to talk"
        " again.{% else %}" + "word " * words + "{% endif %}"
    )
    user, campaign_id, (first, second) = _two_step_linkedin(session_factory, body=body)
    later = _only_message(session_factory, user, campaign_id, second, NOW)
    assert later.body == "Hi Ada, good to talk again." and later.blocked is None
    _approve(session_factory, user, campaign_id, second, later)
    assert _only_message(session_factory, user, campaign_id, second, NOW).approved
    opening = _only_message(session_factory, user, campaign_id, first, NOW)
    assert opening.blocked is not None and opening.blocked.startswith("lint error:")
    with pytest.raises(campaign_review.ReviewConflict, match="can't be approved: lint error"):
        _approve(session_factory, user, campaign_id, first, opening)


def test_an_approved_later_step_message_still_counts_the_next_day(
    session_factory: sessionmaker[Session],
) -> None:
    """The stand-in moves with today, but the fingerprint leaves it out, so a message
    approved today still counts for the activation gate tomorrow."""
    user, campaign_id, (first, second) = _two_step_linkedin(session_factory, body=AGO_BODY)
    for step_id in (first, second):
        message = _only_message(session_factory, user, campaign_id, step_id, NOW)
        _approve(session_factory, user, campaign_id, step_id, message)
    for at in (NOW, NOW + timedelta(days=1)):
        with session_scope(session_factory) as session:
            campaign = campaign_review.get_campaign(session, user, campaign_id)
            gaps = campaign_review.missing(session, user, campaign, now=at)
        assert "message_approvals" not in {gap.requirement for gap in gaps}, at


def test_an_email_test_send_of_a_later_step_shows_the_reviews_ago_words(
    session_factory: sessionmaker[Session],
) -> None:
    """The test send renders with the review's stand-in, so it shows the words the
    review does, not an empty ago."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
        campaign = factories.make_campaign(
            session,
            user,
            channels=(TemplateChannel.EMAIL, TemplateChannel.EMAIL),
            status=CampaignStatus.REVIEWING,
            mailbox_id=mailbox.id,
        )
        for step in campaign.steps:
            template = get_scoped(session, user, Template, step.template_id)
            assert template is not None
            template.body = "It has been {{ previous_send_date | ago }}, {{ first_name }}."
        campaign.steps[1].delay_days = 6
        update_self_contact(session, user, {"first_name": "Selfie"})
        first, second = (
            campaign_review.prepare_test_send(session, user, campaign.id, step.id, today=NOW.date())
            for step in campaign.steps
        )
    assert first.message.get_content().strip() == "It has been , Selfie."
    assert second.message.get_content().strip() == "It has been 6 days ago, Selfie."
