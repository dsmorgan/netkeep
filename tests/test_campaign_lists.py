"""Lists from a campaign's results and the builder's guard preview (#513).

The predicates themselves are tested in ``test_filters.py``; here the buttons on a
campaign's page (one per outcome, with its count) and the list each one makes, and
the preview that counts what each guard would skip before a campaign is saved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import httpx
from campaign_fakes import make_mailbox
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import templates as template_service
from netkeeper.crm import lists as list_service
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.models import (
    CampaignStatus,
    ContactList,
    Enrollment,
    EnrollmentStatus,
    InteractionKind,
    ListKind,
    MessageDirection,
    MessageStatus,
    ResponseVia,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped, scoped

CSRF = {"X-Netkeeper-Client": "1"}
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _seed(app: FastAPI) -> dict[str, Any]:
    """A completed campaign "Autumn hello": two replied (one recorded by hand), three
    with no reply (one of them archived since), one bounced, one opted out."""
    with session_scope(_factory(app), write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(
            session, user, name="Autumn hello", status=CampaignStatus.COMPLETED
        )
        people: dict[str, int] = {}

        def enroll(name: str, **fields: Any) -> Enrollment:
            contact = factories.make_contact(
                session, user, emails=[f"{name}@contacts.example"], **fields.pop("contact", {})
            )
            people[name] = contact.id
            enrollment = factories.make_enrollment(session, campaign, contact, **fields)
            factories.make_message(session, enrollment, sent_at=NOW)  # "no reply" needs a send
            return enrollment

        enroll("quorbelle", status=EnrollmentStatus.REPLIED, replied_at=NOW)
        by_hand = enroll("fictionary", status=EnrollmentStatus.REPLIED, replied_at=NOW)
        factories.make_message(
            session,
            by_hand,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            outside_via=ResponseVia.CALL,
        )
        enroll("silent1", status=EnrollmentStatus.COMPLETED)
        enroll("silent2", status=EnrollmentStatus.COMPLETED)
        enroll("gone", status=EnrollmentStatus.COMPLETED, contact={"archived_at": NOW})
        enroll("bounced", status=EnrollmentStatus.BOUNCED)
        enroll("stop", status=EnrollmentStatus.OPTED_OUT, replied_at=NOW)
        return {"campaign_id": campaign.id, "people": people}


def _counts(body: dict[str, Any]) -> dict[str, int]:
    return {row["outcome"]: row["count"] for row in body["outcomes"]}


def _members(app: FastAPI, list_id: int) -> list[int]:
    with session_scope(_factory(app)) as session:
        page, _ = list_service.list_members(session, _local(session), list_id, limit=100)
        return sorted(c.id for c in page)


async def test_each_outcome_is_counted_as_its_list_would_hold_it(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    response = await client.get(f"/api/v1/campaigns/{seed['campaign_id']}/outcomes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["outcome"] for row in body["outcomes"]] == [
        "replied",
        "no_reply",
        "active",
        "completed",
        "bounced",
        "opted_out",
        "removed",
    ]
    # The archived contact is no list's member, so it is not counted either.
    assert _counts(body) == {
        "replied": 2,
        "no_reply": 2,
        "active": 0,
        "completed": 2,
        "bounced": 1,
        "opted_out": 1,
        "removed": 0,
    }
    assert body["outcomes"][1]["label"] == "no reply"


async def test_a_list_is_a_static_snapshot_named_for_the_campaign_by_default(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    people = seed["people"]
    base = f"/api/v1/campaigns/{seed['campaign_id']}"
    response = await client.post(f"{base}/lists", json={"outcome": "no_reply"}, headers=CSRF)
    assert response.status_code == 201, response.text
    made = response.json()
    assert made["name"] == "Autumn hello: no reply"
    assert made["kind"] == "static"
    assert made["members"] == 2
    assert _members(running_app, made["list_id"]) == sorted([people["silent1"], people["silent2"]])

    # A late reply does not move anyone out of a snapshot.
    with session_scope(_factory(running_app), write=True) as session:
        user = _local(session)
        row = session.scalars(
            scoped(user, Enrollment).where(Enrollment.contact_id == people["silent1"])
        ).one()
        row.replied_at = NOW
    assert _members(running_app, made["list_id"]) == sorted([people["silent1"], people["silent2"]])
    counts = _counts((await client.get(f"{base}/outcomes")).json())
    assert counts["no_reply"] == 1 and counts["replied"] == 3


async def test_a_smart_list_follows_the_campaign(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    people = seed["people"]
    base = f"/api/v1/campaigns/{seed['campaign_id']}"
    made = (
        await client.post(
            f"{base}/lists", json={"outcome": "no_reply", "kind": "smart"}, headers=CSRF
        )
    ).json()
    assert made["kind"] == "smart" and made["members"] == 2
    with session_scope(_factory(running_app)) as session:
        stored = get_scoped(session, _local(session), ContactList, made["list_id"])
        assert stored is not None and stored.kind is ListKind.SMART
        assert stored.filter_json is not None
        assert stored.filter_json["where"] == {
            "op": "campaign_outcome",
            "campaign_ids": [seed["campaign_id"]],
            "outcome": "no_reply",
        }
    with session_scope(_factory(running_app), write=True) as session:
        user = _local(session)
        row = session.scalars(
            scoped(user, Enrollment).where(Enrollment.contact_id == people["silent1"])
        ).one()
        row.replied_at = NOW
    assert _members(running_app, made["list_id"]) == [people["silent2"]]


async def test_replied_counts_a_response_recorded_by_hand(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    made = (
        await client.post(
            f"/api/v1/campaigns/{seed['campaign_id']}/lists",
            json={"outcome": "replied"},
            headers=CSRF,
        )
    ).json()
    people = seed["people"]
    assert _members(running_app, made["list_id"]) == sorted(
        [people["quorbelle"], people["fictionary"]]
    )


async def test_a_taken_default_name_gets_a_number_and_a_taken_chosen_one_is_409(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    url = f"/api/v1/campaigns/{seed['campaign_id']}/lists"
    names = [
        (await client.post(url, json={"outcome": "bounced"}, headers=CSRF)).json()["name"]
        for _ in range(3)
    ]
    assert names == [
        "Autumn hello: bounced",
        "Autumn hello: bounced (2)",
        "Autumn hello: bounced (3)",
    ]
    taken = await client.post(
        url, json={"outcome": "bounced", "name": "Autumn hello: bounced"}, headers=CSRF
    )
    assert taken.status_code == 409
    chosen = await client.post(url, json={"outcome": "bounced", "name": "Follow up"}, headers=CSRF)
    assert chosen.status_code == 201 and chosen.json()["name"] == "Follow up"


async def test_a_default_name_is_stripped_before_the_duplicate_check(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user, name="  Spring hello ")
        factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            status=EnrollmentStatus.BOUNCED,
        )
        campaign_id = campaign.id
    url = f"/api/v1/campaigns/{campaign_id}/lists"
    first = await client.post(url, json={"outcome": "bounced"}, headers=CSRF)
    second = await client.post(url, json={"outcome": "bounced"}, headers=CSRF)
    assert (first.status_code, second.status_code) == (201, 201), second.text
    assert [first.json()["name"], second.json()["name"]] == [
        "Spring hello: bounced",
        "Spring hello: bounced (2)",
    ]


async def test_an_empty_outcome_still_makes_an_empty_list(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    made = await client.post(
        f"/api/v1/campaigns/{seed['campaign_id']}/lists",
        json={"outcome": "removed"},
        headers=CSRF,
    )
    assert made.status_code == 201 and made.json()["members"] == 0


async def test_another_users_campaign_is_404(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        theirs = factories.make_campaign(session, other)
        factories.make_enrollment(session, theirs, factories.make_contact(session, other))
        campaign_id = theirs.id
    assert (await client.get(f"/api/v1/campaigns/{campaign_id}/outcomes")).status_code == 404
    response = await client.post(
        f"/api/v1/campaigns/{campaign_id}/lists", json={"outcome": "active"}, headers=CSRF
    )
    assert response.status_code == 404
    with session_scope(_factory(running_app)) as session:
        names = [row.name for row in session.scalars(scoped(_local(session), ContactList))]
        assert not any("still active" in name for name in names)


# --- the builder's guard preview -----------------------------------------------------


def _preview_seed(app: FastAPI) -> dict[str, Any]:
    """A "no reply" list from a campaign still running, and the contacts' troubles:
    one do-not-contact, one with no email, one contacted yesterday, and one still in
    the running campaign."""
    with session_scope(_factory(app), write=True) as session:
        user = _local(session)
        mailbox = make_mailbox(session, user)
        template_id = template_service.create_template(
            session,
            user,
            name="follow up",
            channel=TemplateChannel.EMAIL,
            subject="Following up",
            body="Hi {{ first_name }}",
        ).id
        old = factories.make_campaign(
            session, user, name="Autumn hello", status=CampaignStatus.COMPLETED
        )
        running = factories.make_campaign(session, user, name="Winter hello")
        fine = factories.make_contact(session, user, emails=["fine@contacts.example"])
        dnc = factories.make_contact(
            session, user, emails=["dnc@contacts.example"], do_not_contact=True
        )
        no_email = factories.make_contact(session, user)
        recent = factories.make_contact(session, user, emails=["recent@contacts.example"])
        busy = factories.make_contact(session, user, emails=["busy@contacts.example"])
        for contact in (fine, dnc, no_email, recent, busy):
            enrollment = factories.make_enrollment(
                session, old, contact, status=EnrollmentStatus.COMPLETED
            )
            # Long enough ago that the old campaign's own send is not "contacted recently".
            factories.make_message(session, enrollment, sent_at=datetime(2025, 1, 1, tzinfo=UTC))
        factories.make_enrollment(session, running, busy, status=EnrollmentStatus.ACTIVE)
        add_interaction(
            session,
            user,
            recent.id,
            InteractionKind.EMAIL_OUT,
            datetime.now(UTC) - timedelta(days=1),
        )
        return {
            "old": old.id,
            "template_id": template_id,
            "mailbox_id": mailbox.id,
        }


async def test_the_preview_counts_each_guard_for_a_list_made_from_a_campaign(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _preview_seed(running_app)
    made = (
        await client.post(
            f"/api/v1/campaigns/{seed['old']}/lists", json={"outcome": "no_reply"}, headers=CSRF
        )
    ).json()
    assert made["members"] == 5

    response = await client.post(
        "/api/v1/campaigns/audience-preview",
        json={"list_id": made["list_id"], "channel": "email"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["audience"] == 5
    assert preview["will_start"] == 1
    assert preview["contacted_within_days"] == 30
    assert {row["reason"]: row["count"] for row in preview["skipped"]} == {
        "do_not_contact": 1,
        "no_email": 1,
        "contacted_recently": 1,
        "in_another_campaign": 1,
    }
    assert sum(row["count"] for row in preview["skipped"]) == 4
    assert preview["summary"].startswith("1 will start, 4 skipped (")

    # The preview says what enrolling will do: the same line, once the campaign exists.
    created = await client.post(
        "/api/v1/campaigns",
        json={
            "name": "Autumn follow-up",
            "mailbox_id": seed["mailbox_id"],
            "steps": [{"template_id": seed["template_id"]}],
            "list_id": made["list_id"],
        },
        headers=CSRF,
    )
    assert created.status_code == 201, created.text
    enrolled = await client.post(
        f"/api/v1/campaigns/{created.json()['id']}/enroll", json={}, headers=CSRF
    )
    assert enrolled.json()["excluded_summary"] == preview["summary"]


async def test_the_preview_takes_a_filter_over_campaign_outcomes(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _preview_seed(running_app)
    tree = {
        "where": {
            "op": "and",
            "children": [
                {"op": "campaign_outcome", "campaign_ids": [seed["old"]], "outcome": "no_reply"},
                {"op": "has_email"},
            ],
        }
    }
    preview = (
        await client.post(
            "/api/v1/campaigns/audience-preview",
            json={"filter": tree, "channel": "email"},
            headers=CSRF,
        )
    ).json()
    assert preview["audience"] == 4
    assert {row["reason"] for row in preview["skipped"]} == {
        "do_not_contact",
        "contacted_recently",
        "in_another_campaign",
    }


async def test_the_preview_needs_one_source_and_the_users_own_list(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    url = "/api/v1/campaigns/audience-preview"
    neither = await client.post(url, json={"channel": "email"}, headers=CSRF)
    assert neither.status_code == 422
    both = await client.post(
        url,
        json={"list_id": 1, "filter": {"where": None}, "channel": "email"},
        headers=CSRF,
    )
    assert both.status_code == 422
    with session_scope(_factory(running_app), write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        theirs = list_service.create_list(session, other, "theirs", ListKind.STATIC)
        list_service.add_members(
            session, other, theirs.id, [factories.make_contact(session, other).id]
        )
        their_list = theirs.id
    response = await client.post(
        url, json={"list_id": their_list, "channel": "email"}, headers=CSRF
    )
    assert response.status_code == 404
