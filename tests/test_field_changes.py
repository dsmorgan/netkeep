"""Per-field change counts for sync and enrichment, and ``linkedin field-changes`` (#518).

Everyone here is invented: Quorbelle Fictionary and Ottoline Madeupson, fake
``ACoAAFAKE`` URNs, fake slugs. The headline question from #149: a sync reads a
card's headline, enrichment reads the profile's, and when the two differ each run
puts its own back. The command counts that from the snapshots the runs wrote, and
prints numbers only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import factories
import pytest
from profile_fakes import Profile, contact_info_of, details_of
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.crm import apply as mapping
from netkeeper.crm import identity
from netkeeper.crm.provenance import set_manual_field
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.connections import ConnectionsPage, SyncMode, SyncResult
from netkeeper.linkedin.connections import StopReason as SyncStop
from netkeeper.linkedin.enrich import EnrichResult, ProfileHarvest
from netkeeper.linkedin.enrich import StopReason as EnrichStop
from netkeeper.linkedin.voyager import ConnectionSummary
from netkeeper.models import (
    Contact,
    ContactSnapshot,
    ContactSource,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import install_scope_guard, scoped
from netkeeper.services import field_changes, runs
from netkeeper.services.connections_sync import SyncRunReport
from netkeeper.services.enrichment import EnrichRunReport
from netkeeper.services.users import ensure_local_user
from netkeeper.services.visit_budget import TodaysVisits

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
QUORBELLE = Profile(n=9518, first="Quorbelle", last="Fictionary", headline="Profile text")
OTTOLINE = Profile(n=9519, first="Ottoline", last="Madeupson", headline="Steady text")
#: Quorbelle again, as her profile reads it with a location (the A, B, C case below).
QUORBELLE_PLACED = Profile(
    n=9518, first="Quorbelle", last="Fictionary", headline="Profile text", location="Invented City"
)
NAMES = ("Quorbelle", "Fictionary", "Ottoline", "Madeupson")


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _stored(session: Session, user: User, profile: Profile, headline: str) -> Contact:
    """``profile`` as an archive import left it: URN, slug, name, and ``headline``."""
    contact = factories.make_contact(
        session,
        user,
        li_urn=profile.urn,
        li_public_id=profile.slug,
        first_name=profile.first,
        last_name=profile.last,
        headline=headline,
        current_title=None,
        current_company=None,
        source=ContactSource.ARCHIVE,
    )
    contact.field_sources = {"headline": "archive"}
    session.flush()
    return contact


def _page(profiles: tuple[Profile, ...], headline: str, *, at: datetime) -> ConnectionsPage:
    """One connections page whose cards all read ``headline``."""
    return ConnectionsPage(
        mode=SyncMode.FULL,
        number=0,
        start=0,
        total=len(profiles),
        connections=tuple(
            ConnectionSummary(
                urn=p.urn,
                public_id=p.slug,
                first_name=p.first,
                last_name=p.last,
                headline=headline,
                connected_at=None,
            )
            for p in profiles
        ),
        observed_at=at,
    )


def _harvest(contact: Contact, profile: Profile, *, at: datetime) -> ProfileHarvest:
    return ProfileHarvest(
        contact_ref=contact.id,
        requested_public_id=profile.slug,
        outcome=Outcome.OK,
        observed_at=at,
        details=details_of(profile),
        contact_info=contact_info_of(profile),
    )


# --- identity.apply_detailed: which job fields changed ------------------------------------


def test_apply_detailed_names_the_job_fields_that_changed(writer: Session, user: User) -> None:
    contact = _stored(writer, user, QUORBELLE, "Archive text")
    incoming = identity.IncomingContact(
        source=ContactSource.SYNC,
        observed_at=NOW,
        li_urn=QUORBELLE.urn,
        first_name=QUORBELLE.first,
        last_name=QUORBELLE.last,
        headline="Card text",
        current_title="Invented Role",
    )

    applied = identity.apply_detailed(writer, user, incoming, identity.Matched(contact.id, "urn"))

    # The title was empty: a first fill is not a change, and writes no snapshot of it.
    assert applied.contact is contact
    assert applied.changed == frozenset({"headline"})
    assert applied.snapshot and len(contact.snapshots) == 1


def test_apply_detailed_reports_no_change_for_a_new_contact_or_the_same_value(
    writer: Session, user: User
) -> None:
    incoming = identity.IncomingContact(
        source=ContactSource.SYNC,
        observed_at=NOW,
        li_urn=OTTOLINE.urn,
        first_name=OTTOLINE.first,
        last_name=OTTOLINE.last,
        headline="Steady text",
    )
    created = identity.apply_detailed(writer, user, incoming, identity.New())
    again = identity.apply_detailed(
        writer, user, incoming, identity.Matched(created.contact.id, "urn")
    )

    assert (created.changed, created.snapshot) == (frozenset(), False)
    assert (again.changed, again.snapshot) == (frozenset(), False)


def test_apply_still_returns_the_contact(writer: Session, user: User) -> None:
    contact = _stored(writer, user, QUORBELLE, "Archive text")
    incoming = identity.IncomingContact(
        source=ContactSource.SYNC, observed_at=NOW, li_urn=QUORBELLE.urn, headline="Card text"
    )

    assert identity.apply(writer, user, incoming, identity.Matched(contact.id, "urn")) is contact


# --- the counts: PageCounts and HarvestCounts ---------------------------------------------


def test_a_sync_counts_its_snapshots_and_headline_changes(writer: Session, user: User) -> None:
    _stored(writer, user, QUORBELLE, "Archive text")
    _stored(writer, user, OTTOLINE, "Card text")

    counts = mapping.apply_page(writer, user, _page((QUORBELLE, OTTOLINE), "Card text", at=NOW))

    # Quorbelle's headline moved; Ottoline's card already said what was stored.
    assert counts.updated == 2
    assert counts.snapshots == 1
    assert counts.field_changes == {"headline": 1, "title": 0, "company": 0, "location": 0}


def test_an_enrichment_counts_its_field_changes_beside_its_snapshots(
    writer: Session, user: User
) -> None:
    contact = _stored(writer, user, QUORBELLE, "Card text")
    counts = mapping.HarvestCounts()

    mapping.apply_harvest(writer, user, _harvest(contact, QUORBELLE, at=NOW), counts)

    assert (counts.applied, counts.snapshots) == (1, 1)
    assert counts.field_changes == {"headline": 1, "title": 0, "company": 0, "location": 0}


def test_a_sync_runs_counts_json_carries_snapshots_and_field_changes() -> None:
    pages = mapping.PageCounts(snapshots=2)
    pages.field_changes["headline"] = 2
    report = SyncRunReport(
        account_id=1,
        result=SyncResult(
            mode=SyncMode.FULL,
            reason=SyncStop.END_OF_LIST,
            pages=1,
            seen_urns=frozenset(),
            total=0,
        ),
        pages=pages,
    )

    counts = report.counts()

    assert counts["snapshots"] == 2
    assert counts["field_changes"] == {"headline": 2, "title": 0, "company": 0, "location": 0}


def test_an_enrichment_runs_counts_json_carries_field_changes_beside_its_harvests() -> None:
    harvests = mapping.HarvestCounts(applied=1, snapshots=1)
    harvests.field_changes["location"] = 1
    report = EnrichRunReport(
        account_id=1,
        run_id=1,
        visits=TodaysVisits(ramp=5, after_weekend=5, after_heat=5, spent_today=0, week_left=None),
        result=EnrichResult(reason=EnrichStop.END_OF_PLAN, planned=1, visits=1, completed=(1,)),
        harvests=harvests,
    )

    counts = report.counts()

    assert counts["field_changes"] == {"headline": 0, "title": 0, "company": 0, "location": 1}
    harvested = counts["harvests"]
    assert isinstance(harvested, dict)
    assert harvested["snapshots"] == 1 and "field_changes" not in harvested


# --- linkedin field-changes ----------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, under tmp_path only."""
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


def _local(session: Session) -> User:
    return ensure_local_user(session, settings=Settings())


Work = Callable[[Session, User, datetime], None]


def _run(factory: sessionmaker[Session], kind: SyncRunKind, at: datetime, work: Work) -> int:
    """One finished run of ``kind`` from ``at`` to ten minutes later; ``work`` writes inside it."""
    with session_scope(factory, write=True) as session:
        user = _local(session)
        run = runs.create_run(session, user, kind, trigger=SyncRunTrigger.MANUAL, now=at)
        work(session, user, at + timedelta(minutes=5))
        runs.finish_run(
            session,
            user,
            run.id,
            status=SyncRunStatus.COMPLETED,
            now=at + timedelta(minutes=10),
            stop_reason="end_of_list",
        )
        return run.id


def _flip_flop_runs(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    """Enrichment reads "Profile text", the card is edited to "Card text", enrichment again.

    A repeat sync of an unchanged card no longer flips the headline (#557); what
    still flips is a card the person edited, and an enrichment that puts the
    profile's text back.
    """
    with session_scope(factory, write=True) as session:
        user = _local(session)
        _stored(session, user, QUORBELLE, "Archive text")
        _stored(session, user, OTTOLINE, "Steady text")
        # The card as the sync last saw it, before the runs below.
        _sync_card(session, user, "Old card text", NOW - timedelta(days=1))

    def sync(session: Session, user: User, at: datetime) -> None:
        _sync_card(session, user, "Card text", at)

    def enrich(session: Session, user: User, at: datetime) -> None:
        for profile in (QUORBELLE, OTTOLINE):
            contact = session.scalars(
                scoped(user, Contact).where(Contact.li_urn == profile.urn)
            ).one()
            mapping.apply_harvest(session, user, _harvest(contact, profile, at=at))

    first = _run(factory, SyncRunKind.ENRICH, NOW, enrich)
    middle = _run(factory, SyncRunKind.CONNECTIONS_FULL, NOW + timedelta(hours=1), sync)
    last = _run(factory, SyncRunKind.ENRICH, NOW + timedelta(days=7), enrich)
    return first, middle, last


def test_field_changes_counts_three_headline_snapshots_and_one_flip_flop(
    cli_db: sessionmaker[Session],
) -> None:
    first, middle, last = _flip_flop_runs(cli_db)

    with session_scope(cli_db) as session:
        report = field_changes.field_changes(session, _local(session), [last, first, middle])

    assert [item.run_id for item in report.runs] == [first, middle, last]
    assert [item.snapshots for item in report.runs] == [1, 1, 1]
    assert sum(item.field_changes["headline"] for item in report.runs) == 3
    assert report.headline_flip_flops == 1


def test_a_single_run_has_no_flip_flop(cli_db: sessionmaker[Session]) -> None:
    first, _, last = _flip_flop_runs(cli_db)

    with session_scope(cli_db) as session:
        report = field_changes.field_changes(session, _local(session), [last])

    assert [item.field_changes["headline"] for item in report.runs] == [1]
    assert report.headline_flip_flops == 0
    with session_scope(cli_db) as session:
        # A to B, and back to A only in a run left out: not a flip-flop across these.
        assert (
            field_changes.field_changes(session, _local(session), [first]).headline_flip_flops == 0
        )


def test_the_command_prints_counts_and_no_names(cli_db: sessionmaker[Session]) -> None:
    first, middle, last = _flip_flop_runs(cli_db)

    shown = CliRunner().invoke(
        cli,
        [
            "linkedin",
            "field-changes",
            "--run",
            str(first),
            "--run",
            str(middle),
            "--run",
            str(last),
        ],
    )

    assert shown.exit_code == 0, shown.output
    lines = shown.output.splitlines()
    assert lines[0].split() == [
        "RUN",
        "KIND",
        "STARTED",
        "(UTC)",
        "SNAPSHOTS",
        "HEADLINE",
        "TITLE",
        "COMPANY",
        "LOCATION",
    ]
    rows = [line.split() for line in lines[1:4]]
    assert [(row[0], row[4], row[5]) for row in rows] == [
        (str(first), "1", "1"),
        (str(middle), "1", "1"),
        (str(last), "1", "1"),
    ]
    assert lines[4] == "headline flip-flops: 1"
    for text in (*NAMES, "Card text", "Profile text", "Archive text", QUORBELLE.slug):
        assert text not in shown.output


def test_the_command_refuses_a_run_that_is_not_there(cli_db: sessionmaker[Session]) -> None:
    shown = CliRunner().invoke(cli, ["linkedin", "field-changes", "--run", "999"])

    assert shown.exit_code == 1
    assert "no run 999" in shown.output


def test_linkedin_run_shows_the_snapshots_and_field_changes(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _local(session)
        run_id = runs.create_run(
            session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
        ).id
        runs.finish_run(
            session,
            user,
            run_id,
            status=SyncRunStatus.COMPLETED,
            now=NOW,
            stop_reason="end_of_list",
            counts={
                "snapshots": 4,
                "field_changes": {"headline": 3, "title": 0, "company": 0, "location": 1},
            },
        )

    shown = CliRunner().invoke(cli, ["linkedin", "run", str(run_id)])

    assert shown.exit_code == 0, shown.output
    assert "field changes  headline 3, title 0, company 0, location 1" in shown.output
    assert "snapshots      4" in shown.output


def test_a_headline_that_only_moves_on_is_not_a_flip_flop(cli_db: sessionmaker[Session]) -> None:
    """Archive, then "Card text", "Profile text", "Third text": every value is new, so 0.

    Quorbelle's location is stored and her profile reads the same one: snapshots carry
    it, and it never changed, so no run counts a location change.
    """
    with session_scope(cli_db, write=True) as session:
        user = _local(session)
        contact = _stored(session, user, QUORBELLE, "Archive text")
        contact.location = "Invented City"
        session.flush()

    def sync(headline: str) -> Work:
        def work(session: Session, user: User, at: datetime) -> None:
            mapping.apply_page(session, user, _page((QUORBELLE,), headline, at=at))

        return work

    def enrich(session: Session, user: User, at: datetime) -> None:
        contact = session.scalars(
            scoped(user, Contact).where(Contact.li_urn == QUORBELLE.urn)
        ).one()
        mapping.apply_harvest(session, user, _harvest(contact, QUORBELLE_PLACED, at=at))

    ids = [
        _run(cli_db, SyncRunKind.CONNECTIONS_FULL, NOW, sync("Card text")),
        _run(cli_db, SyncRunKind.ENRICH, NOW + timedelta(hours=1), enrich),
        _run(cli_db, SyncRunKind.CONNECTIONS_FULL, NOW + timedelta(days=7), sync("Third text")),
    ]

    with session_scope(cli_db) as session:
        report = field_changes.field_changes(session, _local(session), ids)

    assert [item.field_changes["headline"] for item in report.runs] == [1, 1, 1]
    assert [item.field_changes["location"] for item in report.runs] == [0, 0, 0]
    assert report.headline_flip_flops == 0


def test_a_snapshot_another_source_wrote_during_a_run_is_not_the_runs(
    cli_db: sessionmaker[Session],
) -> None:
    """An import's snapshot observed inside a sync's window is the import's, not the sync's.

    It is on Quorbelle, whose sync snapshots bring her timeline into the report.
    """
    first, middle, last = _flip_flop_runs(cli_db)
    with session_scope(cli_db, write=True) as session:
        user = _local(session)
        contact = session.scalars(
            scoped(user, Contact).where(Contact.li_urn == QUORBELLE.urn)
        ).one()
        contact.snapshots.append(
            ContactSnapshot(
                user_id=user.id,
                source=ContactSource.ARCHIVE,
                observed_at=NOW + timedelta(minutes=2),
                headline="Import text",
            )
        )
        session.flush()

    with session_scope(cli_db) as session:
        report = field_changes.field_changes(session, _local(session), [first, middle, last])

    assert [item.snapshots for item in report.runs] == [1, 1, 1]
    assert [item.field_changes["headline"] for item in report.runs] == [1, 1, 1]


# --- the profile headline wins over an unchanged card (#557) -------------------------------


def _sync_card(session: Session, user: User, text: str, at: datetime) -> mapping.PageCounts:
    return mapping.apply_page(session, user, _page((QUORBELLE,), text, at=at))


def _enrich_quorbelle(session: Session, user: User, at: datetime) -> None:
    contact = session.scalars(scoped(user, Contact).where(Contact.li_urn == QUORBELLE.urn)).one()
    mapping.apply_harvest(session, user, _harvest(contact, QUORBELLE, at=at))


def _quorbelle(session: Session, user: User) -> Contact:
    return session.scalars(scoped(user, Contact).where(Contact.li_urn == QUORBELLE.urn)).one()


def _snapshots(session: Session, contact: Contact) -> int:
    session.refresh(contact)
    return len(contact.snapshots)


def test_an_unchanged_card_does_not_overwrite_the_profile_headline(
    writer: Session, user: User
) -> None:
    _stored(writer, user, QUORBELLE, "Archive text")
    _sync_card(writer, user, "Card text", NOW)
    _enrich_quorbelle(writer, user, NOW + timedelta(hours=1))
    contact = _quorbelle(writer, user)
    assert (contact.headline, contact.headline_via) == ("Profile text", "profile")
    before = _snapshots(writer, contact)

    counts = _sync_card(writer, user, "Card text", NOW + timedelta(days=7))

    assert contact.headline == "Profile text"
    assert counts.snapshots == 0
    assert counts.field_changes["headline"] == 0
    assert _snapshots(writer, contact) == before


def test_a_changed_card_wins_over_the_profile_headline(writer: Session, user: User) -> None:
    _stored(writer, user, QUORBELLE, "Archive text")
    _sync_card(writer, user, "Card text", NOW)
    _enrich_quorbelle(writer, user, NOW + timedelta(hours=1))
    _sync_card(writer, user, "Card text", NOW + timedelta(days=1))

    counts = _sync_card(writer, user, "New card text", NOW + timedelta(days=7))

    contact = _quorbelle(writer, user)
    assert (contact.headline, contact.headline_via) == ("New card text", "card")
    assert contact.card_headline_seen == "New card text"
    assert counts.snapshots == 1
    # The next enrichment puts the profile back, and the same card then stays beaten.
    _enrich_quorbelle(writer, user, NOW + timedelta(days=8))
    _sync_card(writer, user, "New card text", NOW + timedelta(days=9))
    assert contact.headline == "Profile text"


def test_a_contact_never_enriched_takes_every_card_change(writer: Session, user: User) -> None:
    _stored(writer, user, QUORBELLE, "Archive text")
    _sync_card(writer, user, "Card text", NOW)
    counts = _sync_card(writer, user, "Newer card text", NOW + timedelta(days=1))

    contact = _quorbelle(writer, user)
    assert (contact.headline, contact.headline_via) == ("Newer card text", "card")
    assert counts.snapshots == 1


def test_a_profile_headline_with_no_card_on_record_counts_as_unchanged(
    writer: Session, user: User
) -> None:
    _stored(writer, user, QUORBELLE, "Archive text")
    _enrich_quorbelle(writer, user, NOW)
    contact = _quorbelle(writer, user)
    assert contact.card_headline_seen is None

    counts = _sync_card(writer, user, "Card text", NOW + timedelta(days=1))

    assert contact.headline == "Profile text"
    assert counts.snapshots == 0
    assert contact.card_headline_seen == "Card text"


def test_a_manual_headline_survives_both_paths(writer: Session, user: User) -> None:
    _stored(writer, user, QUORBELLE, "Archive text")
    _sync_card(writer, user, "Card text", NOW)
    contact = _quorbelle(writer, user)
    set_manual_field(contact, "headline", "My words")

    _enrich_quorbelle(writer, user, NOW + timedelta(hours=1))
    _sync_card(writer, user, "Other card text", NOW + timedelta(days=1))

    assert contact.headline == "My words"
    assert contact.headline_via == "card"  # enrichment did not write it


def test_a_merge_carries_the_markers_of_the_surviving_headline(writer: Session, user: User) -> None:
    _stored(writer, user, QUORBELLE, "Archive text")
    _sync_card(writer, user, "Card text", NOW)
    _enrich_quorbelle(writer, user, NOW + timedelta(hours=1))
    kept = _quorbelle(writer, user)
    empty = factories.make_contact(writer, user, first_name="Ottoline", headline="")

    identity.merge(writer, user, empty.id, kept.id)

    assert (empty.headline, empty.headline_via) == ("Profile text", "profile")
    assert empty.card_headline_seen == "Card text"


def test_a_repeat_sync_after_enrichment_is_no_flip_flop(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        _stored(session, _local(session), QUORBELLE, "Archive text")

    def sync(session: Session, user: User, at: datetime) -> None:
        _sync_card(session, user, "Card text", at)

    def enrich(session: Session, user: User, at: datetime) -> None:
        _enrich_quorbelle(session, user, at)

    runs_ = [
        _run(cli_db, SyncRunKind.CONNECTIONS_FULL, NOW, sync),
        _run(cli_db, SyncRunKind.ENRICH, NOW + timedelta(hours=1), enrich),
        _run(cli_db, SyncRunKind.CONNECTIONS_FULL, NOW + timedelta(days=7), sync),
    ]
    with session_scope(cli_db) as session:
        report = field_changes.field_changes(session, _local(session), runs_)

    assert report.headline_flip_flops == 0
    assert [item.field_changes["headline"] for item in report.runs] == [1, 1, 0]
