"""Lists from a campaign's results, and the builder's guard preview (#513).

A campaign's page offers one button per outcome
(:class:`~netkeep.crm.filters.CampaignOutcome`) with its count: "Replied (7)",
"No reply (23)". Each makes a list of those people, to start a follow-up campaign
from. The count and the list are the same filter, ``campaign_outcome`` over this
one campaign, so a button never promises a number its list does not hold.

- A **static** list (the default) is a snapshot: the people the outcome selects
  now, written as members. A follow-up campaign built from it keeps the audience
  it was shown, however the first campaign's replies move on.
- A **smart** list stores the filter, so it is re-evaluated whenever it is read:
  someone who replies late leaves "no reply" by themselves.

Either way it is an ordinary list. Nothing here sends or enrolls anyone, and a
campaign built from one goes through every guard as any other does (spec 11.9);
:func:`audience_preview` says, before the campaign exists, how many each guard
would skip.

Its name is ``"<campaign>: <outcome>"``, with `` (2)``, `` (3)`` and so on when a
list already has it, cut to fit the column.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from netkeep.crm import lists as crm_lists
from netkeep.crm.filters import (
    OUTCOME_LABELS,
    CampaignOutcome,
    FilterTree,
    compile_count,
    compile_filter,
    parse_filter,
)
from netkeep.models import (
    LIST_NAME_MAX_LENGTH,
    Campaign,
    Contact,
    ContactList,
    ListKind,
    TemplateChannel,
    User,
)
from netkeep.services import campaign_review
from netkeep.services.campaign_guards import (
    REASON_ORDER,
    GuardPolicy,
    Reason,
    Verdict,
    check_contact,
    load_facts,
    reason_label,
    skip_summary,
)
from netkeep.services.campaigns import InvalidCampaign, get_campaign

log = logging.getLogger(__name__)

OUTCOME_ORDER: Final[tuple[CampaignOutcome, ...]] = tuple(CampaignOutcome)
"""The buttons' order: the outcomes as the filter language declares them."""


def outcome_tree(campaign_id: int, outcome: CampaignOutcome) -> FilterTree:
    """The filter behind one button: ``campaign_outcome`` over this campaign alone."""
    return parse_filter(
        {
            "where": {
                "op": "campaign_outcome",
                "campaign_ids": [campaign_id],
                "outcome": outcome.value,
            }
        }
    )


@dataclass(frozen=True, slots=True)
class OutcomeCount:
    outcome: CampaignOutcome
    label: str
    count: int


def outcome_counts(session: Session, user: User, campaign_id: int) -> list[OutcomeCount]:
    """How many contacts each outcome's list would hold now, in :data:`OUTCOME_ORDER`.

    Counted through the filter, so archived and merged-away contacts are left out
    here exactly as the list leaves them out. Reads only.
    """
    campaign = get_campaign(session, user, campaign_id)
    return [
        OutcomeCount(
            outcome=outcome,
            label=OUTCOME_LABELS[outcome],
            count=session.scalar(
                compile_count(user, outcome_tree(campaign.id, outcome), session=session)
            )
            or 0,
        )
        for outcome in OUTCOME_ORDER
    ]


def default_list_name(campaign: Campaign, outcome: CampaignOutcome) -> str:
    """``"<campaign>: <outcome>"``, the campaign's name cut so the whole fits a list name."""
    suffix = f": {OUTCOME_LABELS[outcome]}"
    # Stripped as create_list strips a name, so the duplicate check sees what is stored.
    return campaign.name.strip()[: LIST_NAME_MAX_LENGTH - len(suffix)].rstrip() + suffix


def _free_name(session: Session, user: User, name: str) -> str:
    """``name``, or ``name (2)``, ``name (3)``... whichever no list of the user has."""
    if crm_lists.find_list(session, user, name) is None:
        return name
    n = 2
    while True:
        tail = f" ({n})"
        candidate = name[: LIST_NAME_MAX_LENGTH - len(tail)].rstrip() + tail
        if crm_lists.find_list(session, user, candidate) is None:
            return candidate
        n += 1


@dataclass(frozen=True, slots=True)
class OutcomeList:
    list: ContactList
    members: int


def list_from_outcome(
    session: Session,
    user: User,
    campaign_id: int,
    outcome: CampaignOutcome,
    *,
    kind: ListKind = ListKind.STATIC,
    name: str | None = None,
) -> OutcomeList:
    """A new list of the people ``outcome`` selects in the campaign. Needs a writer session.

    Static by default: a snapshot of who the outcome selects now. ``kind=SMART``
    stores the filter instead, so it is re-evaluated whenever it is read. ``name``
    left out is :func:`default_list_name`, made unique; one given is used as typed,
    and :class:`~netkeep.crm.lists.DuplicateListName` when a list has it.
    """
    campaign = get_campaign(session, user, campaign_id)
    tree = outcome_tree(campaign.id, outcome)
    chosen = (
        _free_name(session, user, default_list_name(campaign, outcome)) if name is None else name
    )
    if kind is ListKind.SMART:
        row = crm_lists.create_list(session, user, chosen, ListKind.SMART, filter=tree)
        members = crm_lists.member_count(session, user, row.id)
    else:
        row = crm_lists.create_list(session, user, chosen, ListKind.STATIC)
        ids = list(
            session.scalars(
                compile_filter(user, tree, session=session)
                .with_only_columns(Contact.id)
                .order_by(Contact.id)
            )
        )
        members = crm_lists.add_members(session, user, row.id, ids) if ids else 0
    log.info(
        "list %d (%s) made from campaign %d's %s: %d members",
        row.id,
        kind.value,
        campaign.id,
        outcome.value,
        members,
    )
    return OutcomeList(list=row, members=members)


# --- the builder's guard preview ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardSkips:
    reason: Reason
    label: str
    count: int


@dataclass(frozen=True, slots=True)
class AudiencePreview:
    audience: int
    """Contacts the source holds now."""
    will_start: int
    """Of those, the ones no guard skips for the first step's channel."""
    skipped: tuple[GuardSkips, ...]
    """Per guard, how many it would skip, each contact counted once under its first
    reason (as :func:`~netkeep.services.campaign_guards.skip_summary` counts), largest
    first. Guards that skip nobody are left out."""
    summary: str
    contacted_within_days: int


def audience_preview(
    session: Session,
    user: User,
    *,
    list_id: int | None,
    filter: FilterTree | None,
    channel: TemplateChannel,
    contacted_within_days: int,
    now: datetime,
) -> AudiencePreview:
    """What enrolling this source would do in a campaign not saved yet. Reads only.

    The same guards :func:`~netkeep.services.campaign_guards.check_enrollment` runs,
    against ``channel`` (the first step's) and ``contacted_within_days`` (what the new
    campaign would copy from the config). With no campaign yet, every live enrollment
    in a running campaign counts as "in another campaign". It gates nothing: the
    guards decide again when the audience is enrolled, and at every send.
    """
    if list_id is not None and filter is not None:
        raise InvalidCampaign("the audience is a list or a filter, not both")
    if list_id is None and filter is None:
        raise InvalidCampaign("give a list or a filter to preview")
    if list_id is not None:
        crm_lists.get_list(session, user, list_id)  # ListNotFound for one not the user's
    filter_json: dict[str, Any] | None = None if filter is None else filter.model_dump(mode="json")
    ids = sorted(
        campaign_review.audience_contact_ids(
            session, user, list_id=list_id, filter_json=filter_json
        )
    )
    facts = load_facts(session, user, ids, campaign_id=None)
    policy = GuardPolicy(contacted_within_days=contacted_within_days)
    verdicts = [check_contact(facts.get(i), i, channel, policy, now=now) for i in ids]
    return AudiencePreview(
        audience=len(ids),
        will_start=sum(1 for v in verdicts if v.eligible),
        skipped=_skips(verdicts, contacted_within_days),
        summary=skip_summary(verdicts, contacted_within_days=contacted_within_days),
        contacted_within_days=contacted_within_days,
    )


def _skips(verdicts: Sequence[Verdict], contacted_within_days: int) -> tuple[GuardSkips, ...]:
    counts: Counter[Reason] = Counter(v.reason for v in verdicts if v.reason is not None)
    ordered = sorted(counts, key=lambda r: (-counts[r], REASON_ORDER.index(r)))
    return tuple(
        GuardSkips(
            reason=r,
            label=reason_label(r, contacted_within_days=contacted_within_days),
            count=counts[r],
        )
        for r in ordered
    )
