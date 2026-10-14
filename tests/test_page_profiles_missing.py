"""PageProfiles: LinkedIn's missing-profile page is spec 9.7's ``NotFound`` (#514).

The CP8c capture (#484) showed that a profile url that does not exist never answers
``404``: the document answers ``200`` with a flight that has no top card, names the
missing-profile page (its router's ``initialPath`` is ``/404/``, its page instance
``urn:li:page:d_flagship3_404``), and the page's own script then moves the tab to
``/404/``. These tests drive the real :class:`~netkeeper.linkedin.page_profiles.PageProfiles`
and :func:`~netkeeper.linkedin.enrich.run_enrichment` over :mod:`profile_site`'s fake
pages, with invented people and invented flight around the captured anchors.

The attacks: can a real profile be read as missing (a top card, a redirect, a path
that only looks like ``/404/``, a flight with no anchor)? Can a missing profile still
be counted as unreadable, stop the run, or cost a scroll? Each guard has a test that
fails when that guard is removed.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import factories
import pytest
from flagship_pages import document_html, missing_profile_payload, profile_payload
from profile_site import LOCATION, ORIGIN, ProfilePage, ProfileSite, Stale
from sqlalchemy.orm import Session, sessionmaker
from test_page_profiles import LOST, NOW, tab_of, target, visit
from time_limit import Stopwatch
from voyager_pages import PEOPLE, Person

from netkeeper.crm.apply import HarvestResult, apply_harvest
from netkeeper.db import session_scope
from netkeeper.linkedin import flagship_profile, page_profiles
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import (
    MAX_UNREADABLE_IN_A_ROW,
    EnrichTarget,
    StopReason,
    UnreadableCause,
)
from netkeeper.linkedin.flagship_profile import has_top_card, is_missing_page
from netkeeper.linkedin.voyager import RouteChanged
from netkeeper.models import Contact, User
from netkeeper.scoping import get_scoped

#: The issue's invented slug, and two more invented people whose profiles are gone.
QUORBELLE = Person(514, "Quorbelle", "Fictionary", None, public_id="quorbelle-fictionary")
ZEPHYRINE = Person(515, "Zephyrine", "Nonesuch", None, public_id="zephyrine-nonesuch")
PRIYA, MATEO = PEOPLE[0], PEOPLE[1]

MISSING_TAB = f"{ORIGIN}/404/"


def missing(person: Person, **overrides: object) -> ProfilePage:
    """``person``'s url answering LinkedIn's missing-profile page; the tab moves after load."""
    values: dict[str, object] = {"landing": "missing", "tab_after_load": MISSING_TAB}
    values.update(overrides)
    return ProfilePage(person, **values)  # type: ignore[arg-type]


# --- the constants and the flight checks -------------------------------------------------


def test_the_missing_page_anchors_are_the_captures() -> None:
    assert flagship_profile.MISSING_PAGE_PATH == "/404/"
    assert flagship_profile.MISSING_PAGE_KEY == "urn:li:page:d_flagship3_404"
    assert flagship_profile.MISSING_PAGE_ROUTE_FIELD == "initialPath"
    assert page_profiles.MISSING_PAGE_POLL_S == 0.25


def test_the_flight_checks_read_the_anchors_and_nothing_else() -> None:
    gone = missing_profile_payload()
    assert not has_top_card(gone) and is_missing_page(gone)
    assert is_missing_page(missing_profile_payload(initial_path=None))
    assert is_missing_page(missing_profile_payload(page_key=None))
    assert not is_missing_page(missing_profile_payload(page_key=None, initial_path=None))
    # Lookalikes are not the anchors.
    assert not is_missing_page(
        missing_profile_payload(page_key="urn:li:page:d_flagship3_4040", initial_path="/404x/")
    )
    profile = profile_payload(PRIYA, location=LOCATION)
    assert has_top_card(profile) and not is_missing_page(profile)
    with pytest.raises(RouteChanged):
        has_top_card(b"<html>not flight</html>")


# --- test 1: the tab moves to /404/ after landing ----------------------------------------


@pytest.mark.wall_clock  # the bound stands for "did not wait out the landing wait"
async def test_the_missing_page_is_not_found_and_no_scroll_is_spent() -> None:
    site = ProfileSite([missing(QUORBELLE)])
    clock = Stopwatch()
    out = await visit(site, [target(QUORBELLE)], landing_wait_s=5.0)
    # The tab's address is read again soon after the document arrived: the visit
    # does not wait out the 5 s landing wait.
    assert clock.elapsed < 4.0
    assert out.outcomes == [Outcome.NOT_FOUND]
    assert out.result.not_found == 1 and out.result.unreadable == 0
    assert out.result.unreadable_visits == ()
    tab = tab_of(site)
    assert tab.mouse.wheels == [] and tab.mouse.moves == []  # no scroll, no pointer rest
    assert site.clicks == [] and out.result.clicks == 0
    # Nothing new was requested: the one document netkeeper navigated to.
    assert site.requests == [("GET", f"/in/{QUORBELLE.slug}/", None)]
    assert tab.goto_calls == [f"{ORIGIN}/in/{QUORBELLE.slug}/"]


@pytest.mark.parametrize(
    ("page_key", "initial_path"),
    [
        pytest.param(flagship_profile.MISSING_PAGE_KEY, None, id="page-key-only"),
        pytest.param(None, flagship_profile.MISSING_PAGE_PATH, id="route-only"),
    ],
)
async def test_either_flight_anchor_with_the_tab_address_is_not_found(
    page_key: str | None, initial_path: str | None
) -> None:
    flight = missing_profile_payload(page_key=page_key, initial_path=initial_path)
    site = ProfileSite([missing(QUORBELLE, screen=flight)])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.NOT_FOUND]


async def test_a_flight_with_no_anchor_is_unreadable_even_at_404() -> None:
    """The address alone is never enough: the document must name the missing page."""
    flight = missing_profile_payload(page_key=None, initial_path=None)
    site = ProfileSite([missing(QUORBELLE, screen=flight)])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.harvests[0].unreadable_cause is UnreadableCause.NO_PROFILE_SCREEN
    assert tab_of(site).mouse.wheels == []


async def test_the_missing_flight_without_the_address_is_unreadable() -> None:
    """The flight alone is never enough: the tab stays at the profile's url."""
    site = ProfileSite([missing(QUORBELLE, tab_after_load=None)])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.harvests[0].unreadable_cause is UnreadableCause.NO_PROFILE_SCREEN
    assert out.result.not_found == 0


async def test_a_lost_missing_document_is_unreadable() -> None:
    """A document whose body could not be read says nothing: unreadable, as before."""
    site = ProfileSite([missing(QUORBELLE, screen_error=LOST)])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.harvests[0].unreadable_cause is UnreadableCause.PROFILE_SCREEN_LOST


# --- test 2: the tab is already at /404/ when goto returns -------------------------------


async def test_the_tab_already_at_404_when_goto_returns_is_not_found() -> None:
    site = ProfileSite([missing(QUORBELLE, tab_after_load=None, tab_on_load=MISSING_TAB)])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.NOT_FOUND]
    assert out.harvests[0].unreadable_cause is None  # not LANDED_OFF_PROFILE
    assert tab_of(site).mouse.wheels == []


async def test_landing_at_404_with_no_missing_document_stays_landed_off_profile() -> None:
    """The tab at /404/ and a document with no anchor: exactly today's cause."""
    flight = missing_profile_payload(page_key=None, initial_path=None)
    page = missing(QUORBELLE, screen=flight, tab_after_load=None, tab_on_load=MISSING_TAB)
    out = await visit(ProfileSite([page]), [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.harvests[0].unreadable_cause is UnreadableCause.LANDED_OFF_PROFILE


# --- test 3: the tab is at /404/, but the document has a top card ------------------------


async def test_404_after_a_profile_screen_landed_is_unreadable() -> None:
    """The guard: a profile screen was accepted, so /404/ after the scroll is LEFT_PROFILE."""
    site = ProfileSite([ProfilePage(PRIYA, tab_after_scroll=MISSING_TAB)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.harvests[0].unreadable_cause is UnreadableCause.LEFT_PROFILE
    assert out.result.not_found == 0


async def test_404_soon_after_a_profile_screen_landed_is_unreadable() -> None:
    """The page moves to /404/ right after a real profile's document: still unreadable."""
    site = ProfileSite([ProfilePage(PRIYA, tab_after_load=MISSING_TAB)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.harvests[0].unreadable_cause is UnreadableCause.LEFT_PROFILE


@pytest.mark.parametrize("landing", ["document", "screen"])
async def test_landing_at_404_with_a_profile_screen_is_landed_off_profile(landing: str) -> None:
    site = ProfileSite([ProfilePage(PRIYA, landing=landing, tab_on_load=MISSING_TAB)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.harvests[0].unreadable_cause is UnreadableCause.LANDED_OFF_PROFILE
    assert tab_of(site).mouse.wheels == []


@pytest.mark.parametrize("when", ["after-load", "after-scroll"])
async def test_a_screen_after_the_missing_document_wins(when: str) -> None:
    """The screen guard: the document is the missing page, then the page's own screen
    request brings this profile's screen, which is accepted. /404/ after that is
    unreadable, never NotFound, though every other condition holds."""
    moves = {"after-load": {"tab_after_load": MISSING_TAB}, "after-scroll": {}}[when]
    page = ProfilePage(
        PRIYA,
        landing="screen",
        document=document_html(missing_profile_payload()),
        tab_after_scroll=MISSING_TAB,
        **moves,  # type: ignore[arg-type]
    )
    out = await visit(ProfileSite([page]), [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0
    assert out.harvests[0].unreadable_cause is UnreadableCause.LEFT_PROFILE


async def test_a_lost_screen_after_the_missing_document_is_unreadable() -> None:
    """The missing document, then this profile's screen request with no body: a screen
    may have been there, so the visit is the lost screen's, never NotFound."""
    page = ProfilePage(
        QUORBELLE,
        landing="screen",
        document=document_html(missing_profile_payload()),
        screen_error=LOST,
        tab_after_load=MISSING_TAB,
    )
    out = await visit(ProfileSite([page]), [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0
    assert out.harvests[0].unreadable_cause is UnreadableCause.PROFILE_SCREEN_LOST


@pytest.mark.parametrize("when", ["on-load", "after-load", "after-scroll"])
async def test_a_top_card_beside_the_missing_anchors_is_still_a_profile(when: str) -> None:
    """A document with a top card is the profile's screen even if it also names the
    missing page: the top card wins, and /404/ then is unreadable, never NotFound."""
    screen = profile_payload(PRIYA, location=LOCATION) + (
        b'ff:{"initialPath":"/404/","pageInstance":"urn:li:page:d_flagship3_404;x=="}\n'
    )
    assert has_top_card(screen) and is_missing_page(screen)
    moves = {
        "on-load": {"tab_on_load": MISSING_TAB},
        "after-load": {"tab_after_load": MISSING_TAB},
        "after-scroll": {"tab_after_scroll": MISSING_TAB},
    }[when]
    page = ProfilePage(PRIYA, document=document_html(screen), **moves)  # type: ignore[arg-type]
    out = await visit(ProfileSite([page]), [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0


# --- test 4: /404/ after a profile-to-profile redirect in this visit ----------------------


async def test_404_after_a_redirect_to_a_missing_profile_is_unreadable() -> None:
    """Priya's url redirects to Quorbelle's, whose page is the missing one."""
    site = ProfileSite([ProfilePage(PRIYA, redirect_to=QUORBELLE.slug), missing(QUORBELLE)])
    out = await visit(site, [target(PRIYA)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0


async def test_404_after_any_followed_redirect_is_unreadable() -> None:
    """The redirect guard alone: this visit followed a redirect for the slug asked for,
    and then the document for that slug is the missing page. Unreadable, never NotFound."""
    renamed = Stale(
        "GET",
        f"/in/{QUORBELLE.slug}/",
        b"",
        status=301,
        headers={"location": f"{ORIGIN}/in/{ZEPHYRINE.slug}/"},
    )
    site = ProfileSite([missing(QUORBELLE)], stale=[renamed])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0
    assert out.harvests[0].unreadable_cause is UnreadableCause.NO_PROFILE_SCREEN


async def test_a_missing_document_for_another_slug_is_not_this_profiles() -> None:
    """A stale missing-page document for another slug, then the tab at /404/: the
    document asked for never said the profile is missing."""
    stale = Stale(
        "GET", f"/in/{ZEPHYRINE.slug}/", document_html(missing_profile_payload()).encode()
    )
    shell = ProfilePage(QUORBELLE, landing="shell", tab_after_load=MISSING_TAB)
    site = ProfileSite([shell], stale=[stale])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0


async def test_404_after_landing_on_another_profile_is_unreadable() -> None:
    """The slug guard: the tab landed on a profile nobody asked for (no redirect), then
    the asked-for document is the missing page and the tab moves to /404/."""
    page = missing(QUORBELLE, tab_on_load=f"{ORIGIN}/in/{ZEPHYRINE.slug}/")
    out = await visit(ProfileSite([page]), [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED] and out.result.not_found == 0


# --- test 5: paths that only look like /404/ ---------------------------------------------


LOOKALIKES = [
    f"{ORIGIN}/404x/",
    f"{ORIGIN}/in/404/",
    f"{ORIGIN}/404",
    f"{ORIGIN}/404/more/",
    f"{ORIGIN}/en/404/",
    "https://www.example.test/404/",
    "http://www.linkedin.com/404/",
]


@pytest.mark.parametrize("url", LOOKALIKES)
@pytest.mark.parametrize("when", ["on-load", "after-load"])
async def test_a_path_that_only_looks_like_404_is_not_not_found(url: str, when: str) -> None:
    moves = (
        {"tab_on_load": url, "tab_after_load": None}
        if when == "on-load"
        else {"tab_after_load": url}
    )
    site = ProfileSite([missing(QUORBELLE, **moves)])
    out = await visit(site, [target(QUORBELLE)])
    assert out.outcomes == [Outcome.ROUTE_CHANGED]
    assert out.result.not_found == 0


# --- test 6: a run over missing profiles, onto the CRM -----------------------------------


def _owner(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    assert user is not None
    return user


async def test_missing_profiles_in_a_row_never_stop_the_run_and_join_the_streak(
    session_factory: sessionmaker[Session],
) -> None:
    gone = [QUORBELLE, ZEPHYRINE]
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        ids = {
            person.n: factories.make_contact(
                session,
                user,
                li_urn=person.urn,
                li_public_id=person.slug,
                first_name=person.first,
                last_name=person.last,
            ).id
            for person in [*gone, PRIYA]
        }
        user_id = user.id
    assert len(gone) >= MAX_UNREADABLE_IN_A_ROW  # unreadable visits like these would stop it

    site = ProfileSite([missing(QUORBELLE), missing(ZEPHYRINE), ProfilePage(PRIYA)])
    targets = [EnrichTarget(ids[p.n], p.slug, p.urn) for p in [*gone, PRIYA]]
    out = await visit(site, targets)

    assert out.result.reason is StopReason.END_OF_PLAN
    assert out.outcomes == [Outcome.NOT_FOUND, Outcome.NOT_FOUND, Outcome.OK]
    assert (out.result.not_found, out.result.unreadable) == (2, 0)
    assert out.result.unreadable_visits == ()

    with session_scope(session_factory, write=True) as session:
        owner = _owner(session, user_id)
        results = [apply_harvest(session, owner, harvest) for harvest in out.harvests]
        assert results[:2] == [HarvestResult.NOT_FOUND, HarvestResult.NOT_FOUND]
        for person in gone:
            contact = get_scoped(session, owner, Contact, ids[person.n])
            assert contact is not None
            assert (contact.li_not_found_count, contact.li_not_found_at) == (1, NOW)
            assert contact.li_not_found_since == NOW and contact.li_disconnected_at is None


async def test_three_missing_visits_across_fourteen_days_mark_the_contact_gone(
    session_factory: sessionmaker[Session],
) -> None:
    """End to end: each missing-page visit is one more on spec 9.8's streak."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        contact_id = factories.make_contact(
            session,
            user,
            li_urn=QUORBELLE.urn,
            li_public_id=QUORBELLE.slug,
            first_name=QUORBELLE.first,
            last_name=QUORBELLE.last,
        ).id
        user_id = user.id

    results = []
    for days in (0, 7, 14):
        out = await visit(
            ProfileSite([missing(QUORBELLE)]),
            [EnrichTarget(contact_id, QUORBELLE.slug, QUORBELLE.urn)],
        )
        (harvest,) = out.harvests
        assert harvest.outcome is Outcome.NOT_FOUND
        at = replace(harvest, observed_at=NOW + timedelta(days=days))
        with session_scope(session_factory, write=True) as session:
            results.append(apply_harvest(session, _owner(session, user_id), at))

    assert results == [HarvestResult.NOT_FOUND, HarvestResult.NOT_FOUND, HarvestResult.GONE]
    with session_scope(session_factory) as session:
        owner = _owner(session, user_id)
        contact = get_scoped(session, owner, Contact, contact_id)
        assert contact is not None
        assert contact.li_disconnected_at is not None
