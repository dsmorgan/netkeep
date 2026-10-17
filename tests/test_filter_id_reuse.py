"""A deleted campaign's or list's id may be handed out again; no stored filter follows it (#574)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import lists as list_service
from netkeeper.crm.filters import (
    FilterTree,
    compile_where,
    describe,
    parse_filter,
    retire_ids,
)
from netkeeper.db import session_scope
from netkeeper.models import Campaign, CampaignStatus, Contact, ListKind, User
from netkeeper.scoping import scoped
from netkeeper.services import campaigns as service


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


def _tree(**where: Any) -> FilterTree:
    return parse_filter({"where": where})


def _matching(session: Session, user: User, tree: FilterTree) -> set[int]:
    clause = compile_where(user, tree, session=session)
    return set(session.scalars(scoped(user, Contact).with_only_columns(Contact.id).where(clause)))


def _stored(where: dict[str, Any]) -> dict[str, Any]:
    return {"where": where, "include_archived": False}


def _enrolled(campaign_id: int) -> dict[str, Any]:
    return {"op": "enrolled_in", "campaign_ids": [campaign_id]}


def test_a_filter_naming_a_deleted_draft_does_not_match_the_campaign_that_reuses_its_id(
    writer: Session,
) -> None:
    user = factories.make_user(writer)
    audience = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    draft = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    draft_id = draft.id
    audience.filter_json = _stored(_enrolled(draft_id))
    smart = list_service.create_list(
        writer, user, "Was in draft", ListKind.SMART, filter=_tree(**_enrolled(draft_id))
    )
    view = list_service.create_view(
        writer, user, "Draft view", ["first_name"], filter=_tree(**_enrolled(draft_id))
    )
    service.delete_campaign(writer, user, draft_id)
    writer.expunge(draft)

    reborn = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    if reborn.id != draft_id:
        pytest.skip("this database does not reuse ids; nothing to guard")
    contact = factories.make_contact(writer, user)
    factories.make_enrollment(writer, reborn, contact)

    for stored in (smart.filter_json, view.filter_json, audience.filter_json):
        assert stored is not None
        tree = parse_filter(stored)
        assert _matching(writer, user, tree) == set()
        assert describe(tree) != ""
    assert smart.filter_json == _stored(_enrolled(-draft_id))


def test_a_filter_naming_a_deleted_list_does_not_match_the_list_that_reuses_its_id(
    writer: Session,
) -> None:
    user = factories.make_user(writer)
    holder = list_service.create_list(
        writer, user, "Holder", ListKind.SMART, filter=_tree(op="is_empty", field="first_name")
    )
    gone = list_service.create_list(writer, user, "Gone", ListKind.STATIC)
    gone_id = gone.id
    holder.filter_json = _stored({"op": "list_member", "list_id": gone_id})
    list_service.delete_list(writer, user, gone_id)
    reborn = list_service.create_list(writer, user, "Reborn", ListKind.STATIC)
    if reborn.id != gone_id:
        pytest.skip("this database does not reuse ids; nothing to guard")
    contact = factories.make_contact(writer, user)
    list_service.add_members(writer, user, reborn.id, [contact.id])

    assert holder.filter_json is not None
    assert _matching(writer, user, parse_filter(holder.filter_json)) == set()


def test_deleting_leaves_filters_naming_other_campaigns_alone(writer: Session) -> None:
    user = factories.make_user(writer)
    kept = factories.make_campaign(writer, user, status=CampaignStatus.COMPLETED)
    draft = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    tree = _tree(op="or", children=[_enrolled(kept.id), _enrolled(draft.id)])
    smart = list_service.create_list(writer, user, "Either", ListKind.SMART, filter=tree)
    draft_id = draft.id
    service.delete_campaign(writer, user, draft_id)
    assert smart.filter_json == _stored(
        {"op": "or", "children": [_enrolled(kept.id), _enrolled(-draft_id)]}
    )
    assert isinstance(writer.get(Campaign, kept.id), Campaign)


def test_another_users_filters_are_not_touched(writer: Session) -> None:
    mine = factories.make_user(writer)
    theirs = factories.make_user(writer)
    draft = factories.make_campaign(writer, mine, status=CampaignStatus.DRAFT)
    other = list_service.create_list(
        writer, theirs, "Theirs", ListKind.SMART, filter=_tree(**_enrolled(draft.id))
    )
    draft_id = draft.id
    service.delete_campaign(writer, mine, draft_id)
    assert other.filter_json == _stored(_enrolled(draft_id))


def test_retire_ids_returns_the_same_object_when_nothing_is_named() -> None:
    stored = _stored(_enrolled(3))
    assert retire_ids(stored, campaign_id=4, list_id=4) is stored
    assert retire_ids(stored, campaign_id=3) == _stored(_enrolled(-3))
    assert stored["where"] == _enrolled(3)
