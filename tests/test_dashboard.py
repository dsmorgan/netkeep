"""The dashboard's own reads (P3-12): next campaign fires, changed jobs, inbound this week,
and the metrics (#738).

Everything else the dashboard shows comes from an endpoint with its own tests
(``/linkedin/*``, ``/mailboxes``). The two-user isolation of these three is in
``tests/isolation``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import factories
import httpx
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from netkeep.crm.filters import compile_filter, parse_filter
from netkeep.crm.interactions import add_interaction
from netkeep.db import session_scope
from netkeep.models import (
    CampaignStatus,
    Contact,
    ContactSnapshot,
    ContactSource,
    EnrollmentStatus,
    InteractionKind,
    MessageStatus,
    TemplateChannel,
    User,
    UserKind,
)
from netkeep.models.base import utcnow
from netkeep.services import campaign_engine
from netkeep.services import dashboard as service

NOW = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
TODAY = date(2026, 9, 16)


def test_the_windows_are_pinned() -> None:
    """Spec 9.8 names the changed-jobs window; "this week" is the last seven days."""
    assert service.CHANGED_JOBS_DAYS == 30
    assert timedelta(days=7) == service.INBOUND_WINDOW
    assert {k.value for k in service.INBOUND_KINDS} == {
        "email_in",
        "li_in",
        "call_in",
        "meeting_in",
        "other_in",
    }


# --- next fires ------------------------------------------------------------------------


def _due(session: Session, user: User, at: datetime, **campaign: Any) -> int:
    row = factories.make_campaign(session, user, **campaign)
    contact = factories.make_contact(session, user)
    return factories.make_enrollment(session, row, contact, next_action_at=at).id


def test_next_fires_are_soonest_first_and_a_due_one_leads(session: Session) -> None:
    user = factories.make_user(session)
    later = _due(session, user, NOW + timedelta(days=2))
    soon = _due(session, user, NOW + timedelta(hours=1))
    overdue = _due(session, user, NOW - timedelta(minutes=5))

    fires, total = campaign_engine.upcoming(session, user, limit=10)

    assert [fire.enrollment.id for fire in fires] == [overdue, soon, later]
    assert total == 3


def test_next_fires_list_only_what_the_tick_would_fire(session: Session) -> None:
    """Status selects, never the due time alone (#242 review); LinkedIn steps wait for P4."""
    user = factories.make_user(session)
    listed = _due(session, user, NOW)
    _due(session, user, NOW, status=CampaignStatus.PAUSED)
    _due(session, user, NOW, channels=(TemplateChannel.LINKEDIN,))
    campaign = factories.make_campaign(session, user)
    for status in (EnrollmentStatus.PAUSED, EnrollmentStatus.PENDING, EnrollmentStatus.REPLIED):
        factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            status=status,
            next_action_at=NOW,
        )
    factories.make_enrollment(session, campaign, factories.make_contact(session, user))

    fires, total = campaign_engine.upcoming(session, user, limit=10)

    assert [fire.enrollment.id for fire in fires] == [listed]
    assert total == 1


def test_next_fires_name_the_next_step(session: Session) -> None:
    user = factories.make_user(session)
    campaign = factories.make_campaign(
        session, user, channels=(TemplateChannel.EMAIL, TemplateChannel.EMAIL)
    )
    contact = factories.make_contact(session, user)
    factories.make_enrollment(session, campaign, contact, current_step=1, next_action_at=NOW)

    [fire], _ = campaign_engine.upcoming(session, user, limit=10)

    assert fire.step is not None
    assert fire.step.position == 2
    assert fire.due == NOW


def test_next_fires_agree_with_the_ticks_own_next_due(session: Session) -> None:
    """The soonest future row is the one the engine's own ``_next_due`` names."""
    user = factories.make_user(session)
    _due(session, user, NOW + timedelta(hours=3))
    _due(session, user, NOW + timedelta(hours=1))
    _due(session, user, NOW + timedelta(hours=1), channels=(TemplateChannel.LINKEDIN,))

    fires, _ = campaign_engine.upcoming(session, user, limit=1)

    assert fires[0].due == campaign_engine._next_due(session, user, NOW)


def test_next_fires_due_now_are_exactly_what_the_tick_selects(session: Session) -> None:
    """#286: ``upcoming`` and the tick share one selection, so they cannot drift.

    Every row the tick's own query (``_due``) would pick up now is listed, and
    every listed row due by now is one the tick would pick up.
    """
    user = factories.make_user(session)
    _due(session, user, NOW - timedelta(hours=1))
    _due(session, user, NOW)
    _due(session, user, NOW + timedelta(hours=1))
    _due(session, user, NOW - timedelta(hours=1), status=CampaignStatus.PAUSED)
    _due(session, user, NOW - timedelta(hours=1), channels=(TemplateChannel.LINKEDIN,))
    campaign = factories.make_campaign(session, user)
    for status in (EnrollmentStatus.PAUSED, EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE):
        factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            status=status,
            next_action_at=NOW - timedelta(minutes=1),
        )
    factories.make_enrollment(session, campaign, factories.make_contact(session, user))

    fires, _ = campaign_engine.upcoming(session, user, limit=50)
    tick = campaign_engine._due(session, user, NOW, seen=(), campaigns=(), mailboxes=())

    listed_due = [fire.enrollment.id for fire in fires if fire.due <= NOW]
    assert listed_due == [enrollment.id for enrollment, _ in tick]
    assert len(listed_due) == 3


def test_a_step_due_exactly_now_is_the_ticks_not_the_next_due(session: Session) -> None:
    """``_next_due`` is the soonest *after* now: a row due at now is this tick's to take."""
    user = factories.make_user(session)
    _due(session, user, NOW)
    later = NOW + timedelta(minutes=5)
    _due(session, user, later)

    assert campaign_engine._next_due(session, user, NOW) == later
    [first, *_], _ = campaign_engine.upcoming(session, user, limit=10)
    assert first.due == NOW
    assert [
        e.id
        for e, _ in campaign_engine._due(session, user, NOW, seen=(), campaigns=(), mailboxes=())
    ] == [first.enrollment.id]


def test_next_fires_limit_keeps_the_total(session: Session) -> None:
    user = factories.make_user(session)
    for hours in range(4):
        _due(session, user, NOW + timedelta(hours=hours))

    fires, total = campaign_engine.upcoming(session, user, limit=2)

    assert len(fires) == 2
    assert total == 4


# --- changed jobs ----------------------------------------------------------------------


def _changed(session: Session, user: User) -> list[int]:
    rows, _ = service.changed_jobs(session, user, now=NOW, limit=50)
    return [row.contact.id for row in rows]


def _noticed(
    session: Session,
    user: User,
    at: datetime,
    *,
    contact_id: int | None = None,
    position_changed: bool = True,
    source: ContactSource = ContactSource.SYNC,
    **overrides: Any,
) -> int:
    """A contact (or ``contact_id``) with a snapshot netkeep wrote at ``at``."""
    contact = (
        factories.make_contact(session, user, **overrides)
        if contact_id is None
        else session.get_one(Contact, contact_id)
    )
    contact.snapshots.append(
        ContactSnapshot(
            user_id=user.id,
            source=source,
            observed_at=at,
            current_title="Before",
            current_company="Old Co",
            position_changed=position_changed,
        )
    )
    session.flush()
    return contact.id


def test_a_position_change_noticed_in_the_last_30_days_is_a_changed_job(
    session: Session,
) -> None:
    user = factories.make_user(session)
    recent = _noticed(session, user, NOW - timedelta(days=3))
    older = _noticed(session, user, NOW - timedelta(days=10))
    edge = _noticed(session, user, NOW - timedelta(days=30))
    _noticed(session, user, NOW - timedelta(days=30, seconds=1))
    _noticed(session, user, NOW + timedelta(minutes=1))  # a clock skew is not "noticed yet"

    assert _changed(session, user) == [recent, older, edge]


def test_a_profile_start_date_alone_is_not_a_changed_job(session: Session) -> None:
    """#286: the card counts what netkeep noticed, not the dates on the profile."""
    user = factories.make_user(session)
    factories.make_contact(session, user, positions=[{"started_on": TODAY - timedelta(days=3)}])
    factories.make_contact(
        session, user, positions=[{"started_on": date(2020, 1, 1), "ended_on": TODAY}]
    )

    assert _changed(session, user) == []


def test_a_new_headline_or_an_imported_job_is_not_a_changed_job(session: Session) -> None:
    """Only a sync's snapshot marked ``position_changed`` counts: an import notices nothing."""
    user = factories.make_user(session)
    _noticed(session, user, NOW - timedelta(days=1), position_changed=False)
    _noticed(session, user, NOW - timedelta(days=1), source=ContactSource.CSV)
    _noticed(session, user, NOW - timedelta(days=1), source=ContactSource.ARCHIVE)
    _noticed(session, user, NOW - timedelta(days=1), source=ContactSource.MANUAL)

    assert _changed(session, user) == []


def test_each_contact_is_listed_once_at_its_latest_notice(session: Session) -> None:
    user = factories.make_user(session)
    contact = _noticed(session, user, NOW - timedelta(days=20))
    _noticed(session, user, NOW - timedelta(days=2), contact_id=contact)
    _noticed(session, user, NOW - timedelta(days=1), contact_id=contact, position_changed=False)

    [row], total = service.changed_jobs(session, user, now=NOW, limit=10)

    assert row.contact.id == contact
    assert total == 1
    assert row.noticed_at == NOW - timedelta(days=2)


def test_archived_merged_and_do_not_contact_are_not_prompts(session: Session) -> None:
    user = factories.make_user(session)
    at = NOW - timedelta(days=1)
    live = _noticed(session, user, at)
    _noticed(session, user, at, archived_at=NOW)
    _noticed(session, user, at, merged_into_id=live)
    _noticed(session, user, at, do_not_contact=True)

    assert _changed(session, user) == [live]


def test_the_card_and_the_crm_filter_agree_on_who_changed_jobs(session: Session) -> None:
    """#313, #323: "changed jobs" means the same on the dashboard and in a smart list."""
    user = factories.make_user(session)
    expected = [
        _noticed(session, user, NOW - timedelta(days=1)),
        _noticed(session, user, NOW - timedelta(days=30)),
    ]
    _noticed(session, user, NOW - timedelta(days=1), position_changed=False)
    _noticed(session, user, NOW - timedelta(days=1), source=ContactSource.ARCHIVE)
    _noticed(session, user, NOW - timedelta(days=31))
    _noticed(session, user, NOW + timedelta(minutes=1))
    tree = parse_filter({"where": {"op": "changed_jobs_within_days", "days": 30}})

    matched = session.scalars(compile_filter(user, tree, session=session, now=NOW))

    assert sorted(_changed(session, user)) == sorted(c.id for c in matched) == sorted(expected)


def test_changed_jobs_limit_keeps_the_total(session: Session) -> None:
    user = factories.make_user(session)
    for days in range(3):
        _noticed(session, user, NOW - timedelta(days=days))

    rows, total = service.changed_jobs(session, user, now=NOW, limit=2)

    assert len(rows) == 2
    assert total == 3


# --- inbound this week -----------------------------------------------------------------


def test_inbound_counts_email_and_linkedin_in_over_the_last_seven_days(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        _inbound(session)


def _inbound(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(session, user)

    def add(kind: InteractionKind, at: datetime) -> None:
        add_interaction(session, user, contact.id, kind, at, "fake")

    add(InteractionKind.EMAIL_IN, NOW - timedelta(days=1))
    add(InteractionKind.LI_IN, NOW - timedelta(days=6, hours=23))
    add(InteractionKind.EMAIL_IN, NOW - timedelta(days=8))
    add(InteractionKind.EMAIL_IN, NOW + timedelta(days=1))
    add(InteractionKind.EMAIL_OUT, NOW - timedelta(days=1))
    add(InteractionKind.LI_OUT, NOW - timedelta(days=1))
    add(InteractionKind.NOTE, NOW - timedelta(days=1))
    # A response recorded from outside netkeep counts too (#512); a logged call does not.
    add(InteractionKind.CALL_IN, NOW - timedelta(days=2))
    add(InteractionKind.MEETING_IN, NOW - timedelta(days=3))
    add(InteractionKind.OTHER_IN, NOW - timedelta(days=4))
    add(InteractionKind.OTHER_IN, NOW - timedelta(days=9))
    add(InteractionKind.CALL, NOW - timedelta(days=1))
    add(InteractionKind.MEETING, NOW - timedelta(days=1))

    count, since = service.inbound_since(session, user, now=NOW)

    assert count == 5
    assert since == NOW - timedelta(days=7)


# --- the endpoints ---------------------------------------------------------------------


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


async def test_next_fires_endpoint_names_the_contact_and_nothing_more(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    due = utcnow() + timedelta(hours=2)
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user, name="Autumn hello")
        contact = factories.make_contact(
            session,
            user,
            first_name="Fictional",
            preferred_name="Fic",
            last_name="Person",
            emails=["fic@example.test"],
        )
        enrollment = factories.make_enrollment(session, campaign, contact, next_action_at=due)
        ids = (enrollment.id, campaign.id, contact.id)

    response = await client.get("/api/v1/dashboard/next-fires")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    [item] = body["items"]
    assert item == {
        "enrollment_id": ids[0],
        "due": item["due"],
        "campaign_id": ids[1],
        "campaign_name": "Autumn hello",
        "step_position": 1,
        "channel": "email",
        "contact_id": ids[2],
        "contact_name": "Fic Person",
        "ready_to_prefill": False,
        "auto_send": False,
    }
    assert datetime.fromisoformat(item["due"]) == due
    assert "fic@example.test" not in response.text


async def test_next_fires_endpoint_bounds_its_limit(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/dashboard/next-fires?limit=0")).status_code == 422
    assert (await client.get("/api/v1/dashboard/next-fires?limit=51")).status_code == 422
    empty = (await client.get("/api/v1/dashboard/next-fires")).json()
    assert empty == {"items": [], "total": 0}


async def test_changed_jobs_endpoint(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    noticed = utcnow() - timedelta(days=4)
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        contact_id = _noticed(
            session,
            user,
            noticed,
            first_name="Fictional",
            last_name="Mover",
            current_title="Head of Tea",
            current_company="Kettle Ltd",
            # Started long ago by the profile's dates: the notice is what counts (#286).
            positions=[{"started_on": date(2025, 1, 1)}],
        )

    body = (await client.get("/api/v1/dashboard/changed-jobs")).json()

    [item] = body["items"]
    assert item == {
        "contact_id": contact_id,
        "contact_name": "Fictional Mover",
        "current_title": "Head of Tea",
        "current_company": "Kettle Ltd",
        "noticed_at": item["noticed_at"],
    }
    assert datetime.fromisoformat(item["noticed_at"]) == noticed
    assert (body["total"], body["days"]) == (1, 30)


async def test_inbound_endpoint_says_replies_are_not_detected_yet(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        contact = factories.make_contact(session, user)
        add_interaction(
            session, user, contact.id, InteractionKind.LI_IN, utcnow() - timedelta(hours=1), "hi"
        )

    body = (await client.get("/api/v1/dashboard/inbound")).json()

    assert body["count"] == 1
    assert body["reply_detection"] is False
    assert datetime.fromisoformat(body["since"]) < utcnow() - timedelta(days=6)


# --- metrics (#738) --------------------------------------------------------------------

ZONE = ZoneInfo("America/New_York")
#: 11:00 on Tuesday 20 October 2026 in New York, 15:00 UTC: the local day and the UTC day
#: agree now, but not at 11:30 PM.
METRICS_NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A New York wall-clock time, aware."""
    return datetime(year, month, day, hour, minute, tzinfo=ZONE)


def _metrics_user(session: Session) -> User:
    return factories.make_user(session, timezone="America/New_York")


def _enrollment(session: Session, user: User, **overrides: Any) -> Any:
    campaign = factories.make_campaign(session, user)
    return factories.make_enrollment(
        session, campaign, factories.make_contact(session, user), **overrides
    )


def _sent(session: Session, user: User, at: datetime, **overrides: Any) -> None:
    factories.make_message(session, _enrollment(session, user), sent_at=at, **overrides)


def _metrics(session: Session, user: User, window: str) -> service.Metrics:
    return service.metrics(session, user, service.MetricsWindow(window), now=METRICS_NOW)


def _series(found: service.Metrics, field: str) -> dict[date, int]:
    """The buckets where ``field`` is not zero."""
    return {b.start: getattr(b, field) for b in found.series if getattr(b, field)}


def test_seven_days_are_the_last_seven_local_days_including_today(session: Session) -> None:
    user = _metrics_user(session)

    found = _metrics(session, user, "7d")

    assert found.start == date(2026, 10, 14)
    assert found.end == date(2026, 10, 20)
    assert [b.start for b in found.series] == [date(2026, 10, d) for d in range(14, 21)]
    assert found.sent == found.replies == found.bounced == found.changed_jobs == 0
    assert found.new_contacts == 0
    assert found.reply_rate is None


def test_a_contact_created_at_11_30_pm_on_the_first_day_counts_on_that_day(
    session: Session,
) -> None:
    """11:30 PM on the 14th in New York is 03:30 UTC on the 15th: the local day wins."""
    user = _metrics_user(session)
    factories.make_contact(session, user, created_at=_at(2026, 10, 14, 23, 30))
    factories.make_contact(session, user, created_at=_at(2026, 10, 13, 23, 59))  # before

    found = _metrics(session, user, "7d")

    assert found.new_contacts == 1
    assert _series(found, "new_contacts") == {date(2026, 10, 14): 1}


def test_new_contacts_are_split_by_first_source(session: Session) -> None:
    user = _metrics_user(session)
    at = _at(2026, 10, 19, 9, 0)
    for source in (ContactSource.SYNC, ContactSource.SYNC, ContactSource.CSV):
        factories.make_contact(session, user, created_at=at, source=source)
    factories.make_contact(session, user, created_at=at, source=ContactSource.ARCHIVE)
    # Added by hand: no bucket of its own, so it counts as imported.
    factories.make_contact(session, user, created_at=at, source=ContactSource.MANUAL)
    survivor = factories.make_contact(session, user, source=ContactSource.MANUAL)
    # A duplicate merged away is not a new person; an archived contact still arrived.
    factories.make_contact(
        session, user, created_at=at, source=ContactSource.SYNC, merged_into_id=survivor.id
    )
    factories.make_contact(session, user, created_at=at, source=ContactSource.SYNC, archived_at=at)
    factories.make_contact(session, user, created_at=at, is_self=True)
    factories.make_contact(session, user, created_at=METRICS_NOW + timedelta(minutes=1))

    found = _metrics(session, user, "7d")

    assert (found.new_contacts, found.new_contacts_linkedin, found.new_contacts_imported) == (
        6,
        3,
        3,
    )
    assert _series(found, "new_contacts") == {date(2026, 10, 19): 6}


def test_sent_counts_what_went_out_on_both_channels_by_sent_at(session: Session) -> None:
    user = _metrics_user(session)
    _sent(session, user, _at(2026, 10, 14, 0, 0))  # the first moment of the window
    _sent(session, user, _at(2026, 10, 13, 23, 59))  # the day before
    _sent(session, user, _at(2026, 10, 20, 10, 0))
    linkedin = factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))
    factories.make_message(
        session,
        factories.make_enrollment(session, linkedin, factories.make_contact(session, user)),
        sent_at=_at(2026, 10, 20, 9, 0),
    )
    _sent(session, user, METRICS_NOW + timedelta(minutes=1))  # a clock skew
    for status in (MessageStatus.SCHEDULED, MessageStatus.DRAFTED, MessageStatus.FAILED):
        _sent(session, user, _at(2026, 10, 18, 9, 0), status=status)

    found = _metrics(session, user, "7d")

    assert found.sent == 3
    assert _series(found, "sent") == {date(2026, 10, 14): 1, date(2026, 10, 20): 2}


def test_a_bounce_counts_when_it_was_found_and_was_still_a_send(session: Session) -> None:
    user = _metrics_user(session)
    # Sent before the window, its bounce found inside it.
    _sent(
        session,
        user,
        _at(2026, 10, 10, 9, 0),
        status=MessageStatus.BOUNCED,
        bounced_at=_at(2026, 10, 15, 22, 0),
    )
    # Sent and bounced in the window; no bounced_at falls back to sent_at.
    _sent(session, user, _at(2026, 10, 16, 9, 0), status=MessageStatus.BOUNCED)

    found = _metrics(session, user, "7d")

    assert found.bounced == 2
    assert _series(found, "bounced") == {date(2026, 10, 15): 1, date(2026, 10, 16): 1}
    assert found.sent == 1
    assert _series(found, "sent") == {date(2026, 10, 16): 1}


def test_replies_count_each_enrollment_once_at_its_first_response(session: Session) -> None:
    user = _metrics_user(session)
    for at in (_at(2026, 10, 17, 9, 0), _at(2026, 10, 17, 23, 0), _at(2026, 10, 19, 9, 0)):
        _enrollment(session, user, status=EnrollmentStatus.REPLIED, replied_at=at)
    _enrollment(session, user, status=EnrollmentStatus.REPLIED, replied_at=_at(2026, 10, 1, 9))
    _enrollment(session, user)
    _sent(session, user, _at(2026, 10, 15, 9, 0))
    _sent(session, user, _at(2026, 10, 15, 10, 0))

    found = _metrics(session, user, "7d")

    assert found.replies == 3
    assert _series(found, "replies") == {date(2026, 10, 17): 2, date(2026, 10, 19): 1}
    # Replies to sends made before the window: the rate can pass 1.
    assert found.reply_rate == 1.5


def test_reply_rate_is_none_when_nothing_was_sent(session: Session) -> None:
    user = _metrics_user(session)
    _enrollment(session, user, status=EnrollmentStatus.REPLIED, replied_at=_at(2026, 10, 17, 9))

    found = _metrics(session, user, "7d")

    assert found.replies == 1
    assert found.reply_rate is None


def test_changed_jobs_are_the_cards_contacts_once_each(session: Session) -> None:
    user = _metrics_user(session)
    mover = _noticed(session, user, _at(2026, 10, 15, 9, 0))
    _noticed(session, user, _at(2026, 10, 18, 23, 30), contact_id=mover)
    _noticed(session, user, _at(2026, 10, 18, 9, 0))
    _noticed(session, user, _at(2026, 10, 18, 9, 0), position_changed=False)
    _noticed(session, user, _at(2026, 10, 18, 9, 0), source=ContactSource.CSV)
    _noticed(session, user, _at(2026, 10, 18, 9, 0), do_not_contact=True)
    _noticed(session, user, _at(2026, 10, 18, 9, 0), archived_at=METRICS_NOW)
    _noticed(session, user, _at(2026, 10, 13, 23, 0))

    found = _metrics(session, user, "7d")

    assert found.changed_jobs == 2
    assert _series(found, "changed_jobs") == {date(2026, 10, 15): 1, date(2026, 10, 18): 2}


def test_thirty_days_have_one_bucket_per_local_day(session: Session) -> None:
    user = _metrics_user(session)
    _sent(session, user, _at(2026, 9, 21, 0, 0))
    _sent(session, user, _at(2026, 9, 20, 23, 59))
    factories.make_contact(session, user, created_at=_at(2026, 9, 21, 23, 30))
    factories.make_contact(session, user, created_at=_at(2026, 10, 1, 0, 30))

    found = _metrics(session, user, "30d")

    assert (found.start, found.end) == (date(2026, 9, 21), date(2026, 10, 20))
    assert len(found.series) == 30
    assert [b.start for b in found.series][:2] == [date(2026, 9, 21), date(2026, 9, 22)]
    assert found.sent == 1
    assert _series(found, "sent") == {date(2026, 9, 21): 1}
    assert _series(found, "new_contacts") == {date(2026, 9, 21): 1, date(2026, 10, 1): 1}


def test_a_year_is_the_last_twelve_local_months_including_this_one(session: Session) -> None:
    user = _metrics_user(session)
    _sent(session, user, _at(2025, 11, 1, 0, 30))
    _sent(session, user, _at(2025, 10, 31, 23, 30))  # last October: before the window
    _sent(session, user, _at(2026, 2, 28, 23, 30))  # 04:30 UTC on 1 March: still February
    _sent(session, user, _at(2026, 10, 20, 9, 0))
    _enrollment(session, user, status=EnrollmentStatus.REPLIED, replied_at=_at(2026, 3, 31, 23))
    _noticed(session, user, _at(2026, 6, 15, 9, 0))
    factories.make_contact(session, user, created_at=_at(2025, 11, 30, 23, 30))

    found = _metrics(session, user, "1y")

    assert (found.start, found.end) == (date(2025, 11, 1), date(2026, 10, 20))
    assert [b.start for b in found.series] == [date(2025, 11, 1), date(2025, 12, 1)] + [
        date(2026, m, 1) for m in range(1, 11)
    ]
    assert found.sent == 3
    assert _series(found, "sent") == {
        date(2025, 11, 1): 1,
        date(2026, 2, 1): 1,
        date(2026, 10, 1): 1,
    }
    assert _series(found, "replies") == {date(2026, 3, 1): 1}
    assert _series(found, "changed_jobs") == {date(2026, 6, 1): 1}
    assert _series(found, "new_contacts") == {date(2025, 11, 1): 1}


def test_a_zone_it_cannot_read_counts_utc_days(session: Session) -> None:
    user = factories.make_user(session, timezone="Not/A_Zone")
    factories.make_contact(session, user, created_at=datetime(2026, 10, 15, 3, 30, tzinfo=UTC))

    found = _metrics(session, user, "7d")

    assert _series(found, "new_contacts") == {date(2026, 10, 15): 1}


def test_metrics_cost_six_statements_whatever_the_window_or_rows(session: Session) -> None:
    """Grouped in the database, not one query per bucket."""
    user = _metrics_user(session)
    for day in range(14, 21):
        _sent(session, user, _at(2026, 10, day, 9, 0))
        _noticed(session, user, _at(2026, 10, day, 9, 0))
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        for window in service.MetricsWindow:
            statements.clear()
            _metrics(session, user, window.value)
            assert len(statements) == 6, (window, statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_metrics_endpoint(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        factories.make_contact(session, user, source=ContactSource.SYNC)
        factories.make_contact(session, user, source=ContactSource.CSV)
        _sent(session, user, utcnow() - timedelta(minutes=5))
        _enrollment(session, user, status=EnrollmentStatus.REPLIED, replied_at=utcnow())

    body = (await client.get("/api/v1/dashboard/metrics")).json()

    assert body["window"] == "7d"
    assert len(body["series"]) == 7
    assert body["end"] == body["series"][-1]["start"]
    assert {k: body[k] for k in ("sent", "replies", "reply_rate", "bounced", "changed_jobs")} == {
        "sent": 1,
        "replies": 1,
        "reply_rate": 1.0,
        "bounced": 0,
        "changed_jobs": 0,
    }
    # The send's and the reply's contacts were added by hand: imported.
    assert body["new_contacts"] == {"total": 4, "linkedin": 1, "imported": 3}
    assert set(body["series"][-1]) == {
        "start",
        "sent",
        "replies",
        "bounced",
        "changed_jobs",
        "new_contacts",
    }
    year = (await client.get("/api/v1/dashboard/metrics?window=1y")).json()
    assert len(year["series"]) == 12
    assert year["start"].endswith("-01")
    assert len((await client.get("/api/v1/dashboard/metrics?window=30d")).json()["series"]) == 30
    assert (await client.get("/api/v1/dashboard/metrics?window=2w")).status_code == 422
