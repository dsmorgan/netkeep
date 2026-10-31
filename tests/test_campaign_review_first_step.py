"""A campaign's first step whose template names ``previous_send_date`` (spec 11.1, 11.8).

The first step never has a previous send in its enrollment, so the field renders empty
("following up on my note from .") or takes the ``else`` branch. The review lints it
as an error and refuses every approval of the step; a later step may name it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import factories
import pytest
from campaign_fakes import NOW
from sqlalchemy.orm import Session, sessionmaker
from test_campaign_review_steps import _linkedin_reviewing

from netkeep.campaigns.render import PREVIOUS_SEND_DATE, LintRule, Part, Severity
from netkeep.campaigns.templates import first_step_errors
from netkeep.db import session_scope
from netkeep.models import (
    Campaign,
    CampaignStatus,
    Contact,
    EnrollmentStatus,
    Template,
    TemplateChannel,
    User,
)
from netkeep.scoping import get_scoped, scoped
from netkeep.services import campaign_review

EMAIL: Final = TemplateChannel.EMAIL
LINKEDIN: Final = TemplateChannel.LINKEDIN

#: Each way a template names the field, as (subject, body) of an email template.
NAMES_IT: Final = {
    "ago": (
        "Hello",
        "Hi {{ first_name }}, following up on my note from {{ previous_send_date | ago }}.",
    ),
    "branch": (
        "Hello",
        "Hi {{ first_name }}, {% if previous_send_date %}as I wrote{% else %}a first note"
        "{% endif %}.",
    ),
    "date": ("Hello", "Hi {{ first_name }}, I last wrote on {{ previous_send_date }}."),
    "subject": ("Since {{ previous_send_date | ago }}", "Hi {{ first_name }}"),
}

FIRST_STEP_MESSAGE: Final = (
    "`previous_send_date` has no value on a campaign's first step, so it renders empty;"
    " use it in a later step only"
)


@dataclass
class Setup:
    factory: sessionmaker[Session]
    user: User
    campaign_id: int
    step_ids: tuple[int, ...]


def _reviewing(
    factory: sessionmaker[Session],
    templates: dict[int, tuple[str | None, str]],
    *,
    channels: tuple[TemplateChannel, ...] = (EMAIL, EMAIL),
    personal_line: bool = False,
) -> Setup:
    """A campaign under review with two contacts pending; ``templates`` sets the
    (subject, body) of the step at each position."""
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        campaign = factories.make_campaign(
            session, user, channels=channels, status=CampaignStatus.REVIEWING
        )
        for step in campaign.steps:
            if step.position in templates:
                template = get_scoped(session, user, Template, step.template_id)
                assert template is not None
                template.subject, template.body = templates[step.position]
                if personal_line:
                    template.body += " {{ personal_line }}"
        for n in (1, 2):
            contact = factories.make_contact(session, user, emails=[f"c{n}@contacts.example"])
            factories.make_enrollment(session, campaign, contact, status=EnrollmentStatus.PENDING)
        return Setup(factory, user, campaign.id, tuple(s.id for s in campaign.steps))


def _review(s: Setup, step_id: int) -> campaign_review.StepReview:
    with session_scope(s.factory) as session:
        return campaign_review.review_step(session, s.user, s.campaign_id, step_id, now=NOW)


def _approve_step(s: Setup, step_id: int) -> None:
    review = _review(s, step_id)
    with session_scope(s.factory, write=True) as session:
        campaign_review.approve_step(
            session, s.user, s.campaign_id, step_id, fingerprint_seen=review.fingerprint, now=NOW
        )


def _lint(s: Setup) -> campaign_review.LintResult:
    with session_scope(s.factory, write=True) as session:
        return campaign_review.record_lint(session, s.user, s.campaign_id, now=NOW)


@pytest.mark.parametrize(("subject", "body"), NAMES_IT.values(), ids=NAMES_IT.keys())
def test_a_first_step_naming_the_previous_send_is_a_lint_error(
    session_factory: sessionmaker[Session], subject: str, body: str
) -> None:
    """The review's lint has the error on step 1, so it is not clean and not recorded."""
    s = _reviewing(session_factory, {1: (subject, body)})
    result = _lint(s)
    assert not result.clean
    issues = dict(result.steps)
    assert issues[2] == ()
    [issue] = issues[1]
    assert (issue.rule, issue.severity, issue.field, issue.message) == (
        LintRule.MISSING_VALUE,
        Severity.ERROR,
        PREVIOUS_SEND_DATE,
        FIRST_STEP_MESSAGE,
    )
    assert issue.part is (Part.SUBJECT if "previous_send_date" in subject else Part.BODY)
    with session_scope(s.factory) as session:
        campaign = get_scoped(session, s.user, Campaign, s.campaign_id)
        assert campaign is not None and campaign.lint_fingerprint is None


@pytest.mark.parametrize(("subject", "body"), NAMES_IT.values(), ids=NAMES_IT.keys())
def test_a_first_step_naming_the_previous_send_blocks_every_message_and_its_approval(
    session_factory: sessionmaker[Session], subject: str, body: str
) -> None:
    """Every message of the step is blocked with the error, and approving the step is
    refused, so no approval covers a message with the hole."""
    s = _reviewing(session_factory, {1: (subject, body)})
    review = _review(s, s.step_ids[0])
    assert review.messages == () and review.total == 0
    assert len(review.blocked) == 2
    for message in review.blocked:
        assert message.blocked == f"lint error: {FIRST_STEP_MESSAGE}"
        assert any(
            i.severity is Severity.ERROR and i.field == PREVIOUS_SEND_DATE for i in message.issues
        )
        assert not message.approved
    with pytest.raises(campaign_review.ReviewConflict, match="step 1 can't be approved: lint"):
        _approve_step(s, s.step_ids[0])
    with session_scope(s.factory) as session:
        assert campaign_review._approvals(session, s.user, s.campaign_id) == []


def test_the_first_step_error_wins_over_a_guard(session_factory: sessionmaker[Session]) -> None:
    """A message a guard also excludes is blocked for the lint error, as any lint error."""
    s = _reviewing(session_factory, {1: NAMES_IT["ago"]})
    with session_scope(s.factory, write=True) as session:
        for contact in session.scalars(scoped(s.user, Contact)):
            contact.do_not_contact = True
    review = _review(s, s.step_ids[0])
    assert {m.blocked for m in review.blocked} == {f"lint error: {FIRST_STEP_MESSAGE}"}


def test_a_later_step_naming_the_previous_send_is_no_error(
    session_factory: sessionmaker[Session],
) -> None:
    """Step 2 has a previous send at its fire: no error, its messages approvable."""
    s = _reviewing(session_factory, {2: NAMES_IT["ago"]})
    assert _lint(s).clean
    review = _review(s, s.step_ids[1])
    assert review.blocked == () and review.total == 2
    assert not any(i.severity is Severity.ERROR for m in review.messages for i in m.issues)
    _approve_step(s, s.step_ids[1])
    assert _review(s, s.step_ids[1]).approved


def test_a_first_step_without_the_field_is_no_error(
    session_factory: sessionmaker[Session],
) -> None:
    s = _reviewing(session_factory, {1: ("Hello", "Hi {{ first_name }}, a first note.")})
    assert _lint(s).clean
    _approve_step(s, s.step_ids[0])
    assert _review(s, s.step_ids[0]).approved


def test_an_earlier_clean_lint_record_no_longer_counts(
    session_factory: sessionmaker[Session],
) -> None:
    """A lint recorded clean before this rule (its fingerprint still current) does not
    let the campaign activate: ``missing`` lints the steps again."""
    s = _reviewing(session_factory, {1: NAMES_IT["ago"]})
    with session_scope(s.factory, write=True) as session:
        campaign = campaign_review.get_campaign(session, s.user, s.campaign_id)
        campaign.lint_checked_at = NOW - timedelta(days=1)
        campaign.lint_fingerprint = campaign_review.content_fingerprint(session, s.user, campaign)
    with session_scope(s.factory) as session:
        campaign = campaign_review.get_campaign(session, s.user, s.campaign_id)
        gaps = campaign_review.missing(session, s.user, campaign, now=NOW)
    [lint_gap] = [m for m in gaps if m.requirement == "lint"]
    assert (lint_gap.detail, lint_gap.step_positions) == ("templates with lint errors", (1,))


def test_a_personal_line_first_step_naming_the_previous_send_refuses_each_message(
    session_factory: sessionmaker[Session],
) -> None:
    """A step whose messages are approved one by one refuses each of them too."""
    s = _reviewing(session_factory, {1: NAMES_IT["ago"]}, personal_line=True)
    review = _review(s, s.step_ids[0])
    assert review.per_message and review.total == 2
    assert all(m.blocked == f"lint error: {FIRST_STEP_MESSAGE}" for m in review.messages)
    seen = {m.enrollment_id: m.fingerprint for m in review.messages}
    with (
        session_scope(s.factory, write=True) as session,
        pytest.raises(campaign_review.ReviewConflict, match="step 1 can't be approved: lint"),
    ):
        campaign_review.approve_messages(
            session, s.user, s.campaign_id, s.step_ids[0], seen, now=NOW
        )
    with session_scope(s.factory) as session:
        assert campaign_review._approvals(session, s.user, s.campaign_id) == []


def test_a_first_linkedin_step_naming_the_previous_send_is_refused(
    session_factory: sessionmaker[Session],
) -> None:
    """The LinkedIn review: blocked, the lint not clean, the approval refused."""
    user, campaign_id = _linkedin_reviewing(
        session_factory,
        subject=None,
        body="Hi {{ first_name }}, it has been {{ previous_send_date | ago }}.",
    )
    with session_scope(session_factory) as session:
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        step_id = campaign.steps[0].id
    s = Setup(session_factory, user, campaign_id, (step_id,))
    review = _review(s, step_id)
    assert review.messages == ()
    assert [m.blocked for m in review.blocked] == [f"lint error: {FIRST_STEP_MESSAGE}"]
    assert not _lint(s).clean
    with pytest.raises(campaign_review.ReviewConflict, match="step 1 can't be approved: lint"):
        _approve_step(s, step_id)


def test_a_linkedin_subject_naming_the_field_is_not_counted(
    session_factory: sessionmaker[Session],
) -> None:
    """A LinkedIn message has no subject (#448): a stored subject naming the field is
    never rendered, so it is no error; the body decides."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        row = Template(
            user_id=user.id,
            name="first step",
            channel=LINKEDIN,
            subject="Since {{ previous_send_date | ago }}",
            body="Hi {{ first_name }}",
            lint_json=[],
        )
        assert first_step_errors(row, 1) == []
        row.body = "Hi {{ first_name }}, since {{ previous_send_date | ago }}"
        assert [i.part for i in first_step_errors(row, 1)] == [Part.BODY]
        assert first_step_errors(row, 2) == []


def test_an_earlier_block_reason_stays_and_the_error_is_listed(
    session_factory: sessionmaker[Session],
) -> None:
    """A message already blocked for its render (here, an empty subject) keeps that
    reason, as with any lint error; the first-step error is still among its issues."""
    s = _reviewing(session_factory, {1: ("{{ previous_send_date | ago }}", "Hi {{ first_name }}")})
    review = _review(s, s.step_ids[0])
    assert {m.blocked for m in review.blocked} == {"renders with no subject"}
    for message in review.blocked:
        assert any(i.message == FIRST_STEP_MESSAGE for i in message.issues)
