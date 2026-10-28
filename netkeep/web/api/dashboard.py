"""``/dashboard``: the reads the dashboard needs that no other resource answers (P3-12).

Everything else on the dashboard comes from the resource that already owns it
(``/linkedin/schedule``, ``/linkedin/budget``, ``/linkedin/heat``,
``/linkedin/status``, ``/linkedin/runs``, ``/mailboxes``), so each number has
one source. See :mod:`netkeep.services.dashboard`.

Read-only and per-user. Nothing here touches the browser or Gmail: every
answer is a database read.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query

from netkeep.models import Contact, StepMode
from netkeep.models.base import utcnow
from netkeep.services import campaign_engine
from netkeep.services import dashboard as service
from netkeep.services.linkedin_steps import effective_mode
from netkeep.web.deps import CurrentUser, SessionDep
from netkeep.web.schemas import (
    ChangedJobOut,
    ChangedJobPage,
    DashboardMetricsOut,
    InboundOut,
    MetricsBucketOut,
    NewContactsOut,
    NextFireOut,
    NextFirePage,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

#: A dashboard card lists a handful; the page behind it has the rest.
MAX_LIMIT: Final = 50


def _name(contact: Contact) -> str:
    first = contact.preferred_name or contact.first_name
    return " ".join(part for part in (first, contact.last_name) if part)


@router.get("/next-fires", operation_id="list_next_fires")
def list_next_fires(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 5,
) -> NextFirePage:
    """The next campaign steps due, soonest first (``next_action_at``, spec 11.4).

    A LinkedIn step is listed too, with ``ready_to_prefill``: a person prefills it
    (P4-09), and the tick never fires it."""
    now = utcnow()
    fires, total = campaign_engine.upcoming(session, user, limit=limit, include_linkedin=True)
    return NextFirePage(
        items=[
            NextFireOut(
                enrollment_id=fire.enrollment.id,
                due=fire.due,
                campaign_id=fire.campaign.id,
                campaign_name=fire.campaign.name,
                step_position=None if fire.step is None else fire.step.position,
                channel=None if fire.step is None else fire.step.channel.value,
                contact_id=fire.contact.id,
                contact_name=_name(fire.contact),
                ready_to_prefill=fire.ready_to_prefill(now),
                auto_send=fire.step is not None
                and effective_mode(fire.step, fire.enrollment) is StepMode.AUTO_SEND,
            )
            for fire in fires
        ],
        total=total,
    )


@router.get("/changed-jobs", operation_id="list_changed_jobs")
def list_changed_jobs(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 10,
) -> ChangedJobPage:
    """Live contacts netkeep saw change position in the last 30 days, newest first (spec 9.8).

    A change is one an enrichment visit noticed (a contact snapshot), dated when
    it was noticed, not the start date on the profile (#286).
    """
    rows, total = service.changed_jobs(session, user, now=utcnow(), limit=limit)
    return ChangedJobPage(
        items=[
            ChangedJobOut(
                contact_id=row.contact.id,
                contact_name=_name(row.contact),
                current_title=row.contact.current_title,
                current_company=row.contact.current_company,
                noticed_at=row.noticed_at,
            )
            for row in rows
        ],
        total=total,
        days=service.CHANGED_JOBS_DAYS,
    )


@router.get("/inbound", operation_id="get_inbound_this_week")
def get_inbound(user: CurrentUser, session: SessionDep) -> InboundOut:
    """Inbound messages in the last seven days. Replies are not detected yet (P3-08)."""
    count, since = service.inbound_since(session, user, now=utcnow())
    return InboundOut(count=count, since=since, reply_detection=False)


@router.get("/metrics", operation_id="get_dashboard_metrics")
def get_metrics(
    user: CurrentUser,
    session: SessionDep,
    window: service.MetricsWindow = service.MetricsWindow.DAYS_7,
) -> DashboardMetricsOut:
    """Five numbers over a rolling window that ends today, with a series (#738).

    ``window`` is ``7d`` or ``30d`` (the last 7 or 30 local days, including today, one
    bucket per day) or ``1y`` (the last 12 months, including this one, one bucket per
    month). Days and months are the user's configured time zone's. Nothing compares
    with an earlier period.

    - ``sent``: outbound campaign messages, email and LinkedIn, that went out (status
      ``sent`` or ``bounced``), by ``sent_at``. Test sends are not counted.
    - ``replies``: enrollments whose first response (``replied_at``: a reply either
      poll found, an opt-out, or a response recorded from outside netkeep) falls in
      the window. An enrollment counts once. Unlike a campaign's results, a response
      dated before the enrollment's first send still counts.
    - ``reply_rate``: ``replies / sent``, or null when nothing was sent. A reply can
      answer a message sent before the window, so the rate can pass 1.
    - ``bounced``: outbound messages marked bounced, by ``bounced_at`` (``sent_at`` for
      a message with none). Every bounced message counts, so an enrollment with two
      bounces counts twice; a campaign's results count only its earliest.
    - ``changed_jobs``: contacts whose title or company a sync's profile visit saw
      change in the window: the contacts the changed-jobs card lists (live, not
      archived, not merged away, not do-not-contact). Once each in the total and in
      each bucket it changed in.
    - ``new_contacts``: contacts created in the window, less merged-away duplicates.
      ``linkedin`` were added by a LinkedIn sync; ``imported`` by an archive or CSV
      import, or by hand.

    Anything dated after now is left out. Read-only.
    """
    found = service.metrics(session, user, window, now=utcnow())
    return DashboardMetricsOut(
        window=found.window,
        start=found.start,
        end=found.end,
        sent=found.sent,
        replies=found.replies,
        reply_rate=found.reply_rate,
        bounced=found.bounced,
        changed_jobs=found.changed_jobs,
        new_contacts=NewContactsOut(
            total=found.new_contacts,
            linkedin=found.new_contacts_linkedin,
            imported=found.new_contacts_imported,
        ),
        series=[
            MetricsBucketOut(
                start=bucket.start,
                sent=bucket.sent,
                replies=bucket.replies,
                bounced=bucket.bounced,
                changed_jobs=bucket.changed_jobs,
                new_contacts=bucket.new_contacts,
            )
            for bucket in found.series
        ],
    )
