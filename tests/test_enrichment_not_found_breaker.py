"""The not-found breaker (#514 review): too many missing profiles stop a run, and count for none.

Once #514 reads LinkedIn's missing-profile page as ``NotFound``, a soft block that
serves that page for every profile would make every visit ``NotFound``, and three
weekly runs would mark every contact in their plans gone. So ``run_enrichment`` stops
as ``many_not_found`` at :data:`~netkeeper.linkedin.enrich.MAX_NOT_FOUND_IN_A_ROW` in a
row, or once more than half of :data:`~netkeeper.linkedin.enrich.NOT_FOUND_SHARE_AFTER`
or more visits are not found. A run that stops that way records none of its not-found
answers as not found: each is an unreadable visit, ``not_found_discounted``.

Driven through the real :func:`~netkeeper.services.enrichment.enrich_contacts` and
:func:`~netkeeper.crm.apply.apply_harvest` over :class:`profile_fakes.FakeBrowser`'s
invented people. No socket, no browser.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from profile_fakes import NOT_FOUND, FakeBrowser, Scripted
from sqlalchemy.orm import Session, sessionmaker
from test_enrichment import NOW, _contact, _enrich, _last_run, _people, _read, _setup

from netkeeper.db import session_scope
from netkeeper.linkedin import enrich
from netkeeper.linkedin.enrich import StopReason, UnreadableCause, UnreadableVisit
from netkeeper.models import Contact, SyncRunStatus, User
from netkeeper.scoping import get_scoped
from netkeeper.services import runs
from netkeeper.services.enrichment import EnrichRunReport


def test_the_not_found_limits_are_pinned() -> None:
    assert enrich.MAX_NOT_FOUND_IN_A_ROW == 4
    assert enrich.NOT_FOUND_SHARE_AFTER == 6
    assert StopReason.MANY_NOT_FOUND.value == "many_not_found"
    assert UnreadableCause.NOT_FOUND_DISCOUNTED.value == "not_found_discounted"


def test_the_stop_reason_has_plain_words() -> None:
    assert runs.describe_stop_reason("many_not_found") == (
        "too many profiles were not found; none was recorded as not found, and they are"
        " visited again later"
    )


def _script(*visits: int) -> dict[int, Scripted]:
    """Visits ``visits`` (numbered from 1) land on a profile that is not found."""
    return {visit - 1: NOT_FOUND for visit in visits}


async def _run(
    factory: sessionmaker[Session], count: int, *not_found: int
) -> tuple[EnrichRunReport, int, list[int]]:
    people = _people(count)
    user_id, ids = _setup(factory, people)
    browser = FakeBrowser.of(people, landing=_script(*not_found))
    report = await _enrich(factory, user_id, browser)
    return report, user_id, [ids[p.n] for p in people]


# --- in a row ------------------------------------------------------------------------


@pytest.mark.parametrize("in_a_row", [2, 3])
async def test_fewer_than_four_in_a_row_go_on_and_are_recorded(
    session_factory: sessionmaker[Session], in_a_row: int
) -> None:
    """#514's own requirement holds: two (and three) missing profiles in a row continue."""
    report, user_id, ids = await _run(session_factory, 5, *range(1, in_a_row + 1))
    assert report.result.reason is StopReason.END_OF_PLAN
    assert report.harvests.not_found == in_a_row and report.harvests.unreadable == 0
    for contact_id in ids[:in_a_row]:
        assert _contact(session_factory, user_id, contact_id).li_not_found_count == 1


async def test_four_in_a_row_stop_the_run_and_count_for_none(
    session_factory: sessionmaker[Session],
) -> None:
    report, user_id, ids = await _run(session_factory, 6, 1, 2, 3, 4)
    result = report.result
    assert result.reason is StopReason.MANY_NOT_FOUND
    assert result.visits == 4 and result.not_found == 4  # what the page said
    assert report.harvests.not_found == 0 and report.harvests.unreadable == 4
    assert result.unreadable_visits == tuple(
        UnreadableVisit(n, ids[n - 1], UnreadableCause.NOT_FOUND_DISCOUNTED) for n in (1, 2, 3, 4)
    )
    for contact_id in ids[:4]:
        contact = _contact(session_factory, user_id, contact_id)
        assert (contact.li_not_found_count, contact.li_not_found_at) == (0, None)
        assert contact.li_disconnected_at is None
        assert contact.li_enrich_attempted_at is not None  # visited, so not due at once
    run = _last_run(session_factory, user_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "many_not_found")


async def test_an_ok_visit_resets_the_streak(session_factory: sessionmaker[Session]) -> None:
    """Three, a profile read, then more: never four in a row. The run stops at visit 6,
    by the share rule (five of six), not by the in-a-row rule at visit 4."""
    report, _, _ = await _run(session_factory, 8, 1, 2, 3, 5, 6, 7)
    assert report.result.reason is StopReason.MANY_NOT_FOUND
    assert report.result.visits == 6  # 5 of 6 not found: the share rule
    report, _, _ = await _run(session_factory, 5, 1, 2, 3, 5)
    assert report.result.reason is StopReason.END_OF_PLAN  # 4 of 5: under six visits


# --- the share -----------------------------------------------------------------------


async def test_more_than_half_of_six_stops_the_run(
    session_factory: sessionmaker[Session],
) -> None:
    """Four of six, never more than two in a row: the share rule, at the sixth visit."""
    report, user_id, ids = await _run(session_factory, 8, 1, 2, 4, 5)
    result = report.result
    assert result.reason is StopReason.MANY_NOT_FOUND and result.visits == 6
    assert report.harvests.not_found == 0 and report.harvests.unreadable == 4
    # The profiles read between them are applied all the same, after being held.
    assert report.harvests.applied == 2
    for index in (2, 5):  # visits 3 and 6
        assert _contact(session_factory, user_id, ids[index]).last_enriched_at is not None
    assert [v.visit for v in result.unreadable_visits] == [1, 2, 4, 5]


async def test_exactly_half_of_six_goes_on(session_factory: sessionmaker[Session]) -> None:
    report, _, _ = await _run(session_factory, 8, 1, 2, 4)
    assert report.result.reason is StopReason.END_OF_PLAN
    assert report.harvests.not_found == 3


async def test_the_share_rule_waits_for_six_visits(session_factory: sessionmaker[Session]) -> None:
    """Four of five, in a run of five: the share rule never applies before six visits."""
    report, _, _ = await _run(session_factory, 5, 1, 2, 4, 5)
    assert report.result.reason is StopReason.END_OF_PLAN
    assert report.harvests.not_found == 4


# --- nothing marked gone -------------------------------------------------------------


async def test_a_run_that_trips_the_breaker_marks_nobody_gone(
    session_factory: sessionmaker[Session],
) -> None:
    """Every contact is one not-found visit from gone (two, over 14 days ago). A run that
    trips the breaker leaves every streak where it was."""
    people = _people(6)
    user_id, ids = _setup(session_factory, people)
    since = NOW - timedelta(days=20)

    def arm(session: Session, user: User) -> None:
        for person in people:
            contact = get_scoped(session, user, Contact, ids[person.n])
            assert contact is not None
            contact.li_not_found_count = 2
            contact.li_not_found_since = since
            contact.li_not_found_at = since

    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        arm(session, user)

    browser = FakeBrowser.of(people, landing=_script(1, 2, 3, 4, 5, 6))
    report = await _enrich(session_factory, user_id, browser)

    assert report.result.reason is StopReason.MANY_NOT_FOUND
    assert report.harvests.gone == 0 and report.harvests.not_found == 0
    for person in people:
        contact = _contact(session_factory, user_id, ids[person.n])
        assert contact.li_disconnected_at is None
        assert (contact.li_not_found_count, contact.li_not_found_since) == (2, since)

    def notes(session: Session, user: User) -> int:
        return sum(
            len(c.interactions)
            for c in (get_scoped(session, user, Contact, ids[p.n]) for p in people)
            if c is not None
        )

    assert _read(session_factory, user_id, notes) == 0


async def test_without_the_breaker_the_same_contact_would_be_marked_gone(
    session_factory: sessionmaker[Session],
) -> None:
    """The control: one not-found visit, under the limits, does mark it gone."""
    people = _people(2)
    user_id, ids = _setup(session_factory, people)
    since = NOW - timedelta(days=20)
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        contact = get_scoped(session, user, Contact, ids[people[0].n])
        assert contact is not None
        contact.li_not_found_count, contact.li_not_found_since = 2, since

    report = await _enrich(session_factory, user_id, FakeBrowser.of(people, landing=_script(1)))
    assert report.result.reason is StopReason.END_OF_PLAN and report.harvests.gone == 1
    assert _contact(session_factory, user_id, ids[people[0].n]).li_disconnected_at == NOW
