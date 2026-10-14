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

import itertools
import random
from datetime import datetime, timedelta
from typing import Any

import pytest
from profile_fakes import (
    BAD_REQUEST,
    CHECKPOINT,
    LOGGED_OUT,
    NOT_FOUND,
    THROTTLED,
    FakeBrowser,
    Scripted,
)
from sqlalchemy.orm import Session, sessionmaker
from test_enrichment import (
    NOW,
    _cancel_after,
    _contact,
    _enrich,
    _last_run,
    _people,
    _read,
    _setup,
)

from netkeeper.config import BudgetSettings, LinkedInSettings
from netkeeper.crm import apply as mapping
from netkeeper.crm.apply import HarvestResult
from netkeeper.db import session_scope
from netkeeper.linkedin import enrich
from netkeeper.linkedin.browser import BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import StopReason, UnreadableCause, UnreadableVisit
from netkeeper.models import Contact, SyncRunStatus, User
from netkeeper.scoping import get_scoped
from netkeeper.services import enrich_plan, route_breaker, runs
from netkeeper.services.enrichment import STREAK_STOPS, EnrichRunReport


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


#: An unreadable landing: the tab somewhere that is not a profile.
UNREADABLE = Scripted(
    Outcome.ROUTE_CHANGED, unparsed=True, cause=UnreadableCause.LANDED_OFF_PROFILE
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


# --- #514 review: the run's ending decides the streaks, and nothing is held ----------


def _arm_streak(
    factory: sessionmaker[Session], user_id: int, contact_id: int, count: int, since: datetime
) -> None:
    with session_scope(factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        contact = get_scoped(session, user, Contact, contact_id)
        assert contact is not None
        contact.li_not_found_count, contact.li_not_found_since = count, since


async def test_a_browser_lost_after_a_not_found_keeps_every_profile_read(
    session_factory: sessionmaker[Session],
) -> None:
    """The review's probe: visit 1 not found, visits 2-4 read, Chrome gone on visit 5.
    Every profile read is saved as it arrived, the plan says so (nothing is visited again
    early), and the run that ended by exception adds no not-found streak."""
    people = _people(6)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people, landing=_script(1))

    def chrome_goes(kind: str, value: object) -> None:
        if kind == "goto" and len(browser.visited()) == 5:
            raise BrowserUnavailable("lost the tab while finding Contact info")

    browser.on_event = chrome_goes
    with pytest.raises(BrowserUnavailable):
        await _enrich(session_factory, user_id, browser)

    lost = _last_run(session_factory, user_id)
    plan = _read(session_factory, user_id, lambda s, u: enrich_plan.load_plan(s, u, lost.id))
    assert lost.status is SyncRunStatus.FAILED
    assert plan.completed == tuple(ids[p.n] for p in people[:4])
    for person in people[1:4]:
        contact = _contact(session_factory, user_id, ids[person.n])
        assert contact.last_enriched_at == NOW and contact.headline == person.headline
    missing = _contact(session_factory, user_id, ids[people[0].n])
    assert missing.li_enrich_attempted_at == NOW  # an attempt: not due again at once
    assert (missing.li_not_found_count, missing.li_not_found_at) == (0, None)
    # A resume of that run visits only what it never reached.
    again = FakeBrowser.of(people)
    await _enrich(session_factory, user_id, again, resume_of=lost.id)
    assert again.visited() == [p.slug for p in people[4:]]


async def test_a_cancel_mid_run_adds_no_streak(session_factory: sessionmaker[Session]) -> None:
    people = _people(5)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(
        people, landing=_script(1), on_event=_cancel_after(session_factory, user_id, 1)
    )
    report = await _enrich(session_factory, user_id, browser)
    assert report.result.reason is StopReason.CANCELLED and report.result.not_found == 1
    missing = _contact(session_factory, user_id, ids[people[0].n])
    assert missing.li_not_found_count == 0 and missing.li_enrich_attempted_at == NOW


async def test_a_natural_end_adds_each_streak_once(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    people = _people(4)
    user_id, ids = _setup(session_factory, people)
    since = NOW - timedelta(days=20)
    _arm_streak(session_factory, user_id, ids[people[2].n], 2, since)
    calls: list[int] = []
    real = mapping.add_not_found_streaks

    def counted(*args: Any, **kwargs: Any) -> list[HarvestResult]:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(mapping, "add_not_found_streaks", counted)
    report = await _enrich(session_factory, user_id, FakeBrowser.of(people, landing=_script(1, 3)))

    assert report.result.reason is StopReason.END_OF_PLAN and calls == [1]
    first = _contact(session_factory, user_id, ids[people[0].n])
    assert (first.li_not_found_count, first.li_not_found_at) == (1, NOW)
    third = _contact(session_factory, user_id, ids[people[2].n])
    assert third.li_disconnected_at == NOW  # its third, across 14 days: gone
    assert (report.harvests.not_found, report.harvests.gone) == (1, 1)
    run = _last_run(session_factory, user_id)
    assert run.counts_json is not None
    assert run.counts_json["harvests"]["gone"] == 1


@pytest.mark.parametrize("ending", ["many_not_found", "already_ended"])
async def test_no_streak_is_added_after_many_not_found_or_twice(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """``many_not_found`` adds none. Neither does a run whose row was already ended by the
    time its ending is written: the streaks go with the ending, once."""
    people = _people(6)
    user_id, ids = _setup(session_factory, people)
    calls: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> list[HarvestResult]:
        calls.append(1)
        return []

    monkeypatch.setattr(mapping, "add_not_found_streaks", counted)
    if ending == "many_not_found":
        browser = FakeBrowser.of(people, landing=_script(1, 2, 3, 4))
    else:
        # The run reaches its own end, and its row is ended elsewhere just before its
        # ending is written (the step before it is the Contact info breaker's).
        browser = FakeBrowser.of(people, landing=_script(1))
        real = route_breaker.record_contact_info

        def then_end_it(session: Session, user: User, account_id: int, **kwargs: Any) -> Any:
            done = real(session, user, account_id, **kwargs)
            running = runs.running_run(session, user, account_id)
            assert running is not None
            runs.finish_run(session, user, running.id, status=SyncRunStatus.ABORTED, now=NOW)
            return done

        monkeypatch.setattr(route_breaker, "record_contact_info", then_end_it)
    report = await _enrich(session_factory, user_id, browser)
    assert report.result.reason is StopReason(
        ending if ending != "already_ended" else "end_of_plan"
    )
    assert calls == []
    assert _contact(session_factory, user_id, ids[people[0].n]).li_not_found_count == 0


async def test_the_discounted_visits_are_recorded_in_visit_order(
    session_factory: sessionmaker[Session],
) -> None:
    """An unreadable visit among them keeps its place: the run's progress record and its
    counts list visits 1-5 in order, each with its own code."""
    people = _people(7)
    user_id, ids = _setup(session_factory, people)
    landing = {**_script(1, 2, 4, 5, 6), 2: UNREADABLE}  # visit 3 unreadable
    report = await _enrich(session_factory, user_id, FakeBrowser.of(people, landing=landing))
    assert report.result.reason is StopReason.MANY_NOT_FOUND
    run = _last_run(session_factory, user_id)
    want = [
        {"visit": n, "contact_id": ids[people[n - 1].n], "reason": reason}
        for n, reason in [
            (1, "not_found_discounted"),
            (2, "not_found_discounted"),
            (3, "landed_off_profile"),
            (4, "not_found_discounted"),
            (5, "not_found_discounted"),
            (6, "not_found_discounted"),
        ]
    ]
    assert run.progress_json is not None and run.counts_json is not None
    assert run.progress_json["unreadable_visits"] == want
    assert run.counts_json["unreadable_visits"] == want


# --- N2: the breaker is checked before the unreadable limits ------------------------


def _model(sequence: str) -> tuple[StopReason, int]:
    """How a run of ``sequence`` (``N`` not found, ``U`` unreadable, ``O`` read) stops,
    the breaker checked first, then the unreadable limits, after every visit."""
    not_found = in_a_row = unreadable = unreadable_in_a_row = 0
    for visit, kind in enumerate(sequence, start=1):
        in_a_row = in_a_row + 1 if kind == "N" else 0
        not_found += kind == "N"
        unreadable_in_a_row = unreadable_in_a_row + 1 if kind == "U" else 0
        unreadable += kind == "U"
        if in_a_row >= 4 or (visit >= 6 and 2 * not_found > visit):
            return StopReason.MANY_NOT_FOUND, visit
        if kind == "U" and (unreadable_in_a_row >= 2 or unreadable >= 3):
            return StopReason.RESPONSE, visit
    return StopReason.END_OF_PLAN, len(sequence)


async def test_every_seven_visit_run_stops_as_the_breaker_first_model_says() -> None:
    """All 3^7 runs of not found, unreadable, and read visits, through the real job: each
    stops where the model with the breaker checked first says. No run meets both rules
    on one visit (an unreadable visit ends a not-found streak, and the share rule first
    applies at visit 6), so the order is pinned by the model and by this count."""
    people = _people(7)
    both = 0
    for sequence in map("".join, itertools.product("NUO", repeat=7)):
        landing = {
            i: NOT_FOUND if kind == "N" else UNREADABLE
            for i, kind in enumerate(sequence)
            if kind != "O"
        }
        result = await enrich.run_enrichment(
            enrich.EnrichJobSpec(
                targets=tuple(enrich.EnrichTarget(p.n, p.slug, p.urn) for p in people),
                visit_budget=7,
            ),
            FakeBrowser.of(people, landing=landing).source(),
            _Open(),
            on_harvest=_ignore,
            rng=random.Random(1),
            clock=lambda: NOW,
        )
        assert (result.reason, result.visits) == _model(sequence), sequence
        stop, visit = _model(sequence)
        prefix = sequence[:visit]
        if stop is StopReason.MANY_NOT_FOUND and prefix[-1] == "U":
            u = prefix.count("U")
            both += u >= 3 or prefix.endswith("UU")
    assert both == 0


class _Open:
    async def before_visit(self, number: int) -> StopReason | None:
        return None

    async def pause(self, seconds: float) -> bool:
        return True


async def _ignore(harvest: object) -> None:
    return None


# --- which stops add streaks -----------------------------------------------------------

#: Every stop reason, and whether a run that stops with it adds its not-found streaks.
#: A new StopReason fails the test below until it is classified here (and, if it should
#: add streaks, listed in STREAK_STOPS); unlisted, it adds none.
ADDS_STREAKS: dict[StopReason, bool] = {
    StopReason.END_OF_PLAN: True,
    StopReason.VISIT_BUDGET: True,
    StopReason.BUDGET: True,
    StopReason.INACTIVE: True,
    StopReason.CANCELLED: False,
    StopReason.RESPONSE: False,  # a checkpoint, a throttle, a sign-out, a changed route
    StopReason.ANSWER_LOST: False,
    StopReason.MANY_NOT_FOUND: False,
}


@pytest.mark.parametrize("reason", list(StopReason), ids=lambda reason: reason.value)
def test_every_stop_reason_is_classified_for_streaks(reason: StopReason) -> None:
    assert reason in ADDS_STREAKS, f"classify {reason.value!r}: does it add not-found streaks?"
    assert (reason in STREAK_STOPS) is ADDS_STREAKS[reason]


def test_the_streak_stops_are_only_the_classified_ones() -> None:
    assert {reason for reason, adds in ADDS_STREAKS.items() if adds} == STREAK_STOPS


@pytest.mark.parametrize(
    "stop",
    [
        pytest.param(CHECKPOINT, id="checkpoint"),
        pytest.param(THROTTLED, id="throttled"),
        pytest.param(LOGGED_OUT, id="logged-out"),
        pytest.param(BAD_REQUEST, id="route-changed"),
    ],
)
async def test_a_stop_linkedin_caused_adds_no_streak(
    session_factory: sessionmaker[Session], stop: Scripted
) -> None:
    """Visit 1 not found, then LinkedIn's own answer stops the run: that run's pages are
    suspect, so its not-found visit is an attempt only."""
    people = _people(4)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people, landing={**_script(1), 1: stop})
    report = await _enrich(session_factory, user_id, browser)
    assert report.result.reason is StopReason.RESPONSE and report.result.not_found == 1
    missing = _contact(session_factory, user_id, ids[people[0].n])
    assert missing.li_not_found_count == 0 and missing.li_enrich_attempted_at == NOW


async def test_a_budget_stop_adds_the_streaks(session_factory: sessionmaker[Session]) -> None:
    """A limit of netkeeper's own says nothing about LinkedIn's pages: the streak is added."""
    people = _people(4)
    user_id, ids = _setup(session_factory, people)
    report = await _enrich(
        session_factory,
        user_id,
        FakeBrowser.of(people, landing=_script(1)),
        settings=LinkedInSettings(budget=BudgetSettings(profile_visits_per_day=2, warmup_start=2)),
    )
    assert report.result.reason in STREAK_STOPS and report.result.visits <= 2
    assert _contact(session_factory, user_id, ids[people[0].n]).li_not_found_count == 1
