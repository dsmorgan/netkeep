"""Where first-run setup stands: the ordered steps, their state and what gates them (#651, #667).

One answer to "where is this person in setup?", for the dashboard's Steps tab. Every
state is read from what netkeep already stores or can see without a probe: the
database, netkeep's Chrome profile lock file
(:func:`netkeep.services.posture.netkeep_chrome_pid`, #599) and the login item's
plist and ``launchctl print`` (read-only, as ``/login-item`` reads it). The Keychain is
never read. Nothing here attaches to Chrome, calls Gmail, or starts a run. The first
LinkedIn inbox poll stays one a person starts (``FIRST_POLL_BY_HAND`` in
:mod:`netkeep.services.scheduled_runs`): this reports it as to do, and never starts it.

**The graph.** A step's ``needs`` must all be done, and when it has ``needs_any``, one
of those must be done too; until then it is ``blocked``. Every edge is one the code
enforces, or one the spec makes a person's rule:

- The first sync and the first inbox read need a signed-in LinkedIn session in
  netkeep's Chrome: every run attaches to it and refuses without one.
- Triage and building a list need contacts. A list is any filter over contacts, so it
  does not wait for triage to finish: a large network may never reach zero untriaged.
- A campaign needs contacts to enroll, and a channel to reach them by: Gmail or
  LinkedIn. Its audience is a list or a filter (spec 11.8), so it does not need a list.
- Arming scheduled runs is a person's decision after a supervised run (P2-10, CP4), so
  it waits for the first sync.

**Precedence.** ``done`` wins over everything: a step that is skipped and later done
reads done. ``blocked`` comes next, so a skip never hides that a prerequisite is
missing. Then ``skipped``, then ``in_progress`` or ``todo``. A skip is per user, in one
``settings_kv`` row (:data:`SKIPPED_KEY`), and changes neither ``done`` nor ``blocked``:
a dependent of a skipped step stays blocked. ``skipped`` on a step says whether the
person skipped it, whatever its state, so the page can offer to un-skip.

**Next.** Every step in progress is next, and so is the first step in order that is
to do and not skipped: the step or steps to do now.

Read-only, except :func:`skip` and :func:`unskip`.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from sqlalchemy import func
from sqlalchemy.orm import Session

from netkeep.crm.contacts import contact_stats
from netkeep.crm.self_contact import get_self_contact
from netkeep.models import (
    Campaign,
    CampaignStatus,
    ContactList,
    ImportRun,
    ImportStatus,
    Mailbox,
    MailboxStatus,
    SyncRunKind,
    SyncRunStatus,
    User,
)
from netkeep.scoping import scoped
from netkeep.services.gmail_setup import load as load_gmail_setup
from netkeep.services.linkedin_accounts import account_id_for, scheduled_runs_armed
from netkeep.services.linkedin_session import last_session_evidence, session_flag
from netkeep.services.login_item import State as LoginItemState
from netkeep.services.login_item import Status as LoginItemStatus
from netkeep.services.runs import has_completed_inbox_poll, latest_run
from netkeep.services.settings_kv import get_setting, set_setting

#: The ``settings_kv`` key holding the keys of the steps a person skipped.
SKIPPED_KEY: Final = "setup.skipped"


class StepState(enum.StrEnum):
    DONE = "done"
    IN_PROGRESS = "in_progress"
    TODO = "todo"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class StepKey(enum.StrEnum):
    """The steps, in the order the page lists them."""

    ABOUT_YOU = "about_you"
    IMPORT = "import"
    GMAIL = "gmail"
    CHROME = "chrome"
    FIRST_SYNC = "first_sync"
    FIRST_INBOX = "first_inbox"
    TRIAGE = "triage"
    BUILD_LIST = "build_list"
    CAMPAIGN = "campaign"
    ARM_SCHEDULE = "arm_schedule"
    LOGIN_ITEM = "login_item"


@dataclass(frozen=True, slots=True)
class StepDef:
    key: StepKey
    title: str
    target: str
    """The UI route that handles the step. Only a page: nothing here starts a run."""
    needs: tuple[StepKey, ...] = ()
    needs_any: tuple[StepKey, ...] = ()
    optional: bool = False


#: The graph. Each edge is justified in the module docstring.
STEPS: Final[tuple[StepDef, ...]] = (
    StepDef(StepKey.ABOUT_YOU, "About you", "/settings"),
    StepDef(StepKey.IMPORT, "Import contacts", "/imports"),
    StepDef(StepKey.GMAIL, "Connect Gmail", "/settings"),
    StepDef(StepKey.CHROME, "Start netkeep's Chrome and sign in to LinkedIn", "/linkedin"),
    StepDef(StepKey.FIRST_SYNC, "First LinkedIn sync", "/linkedin", needs=(StepKey.CHROME,)),
    StepDef(StepKey.FIRST_INBOX, "First inbox read", "/linkedin", needs=(StepKey.CHROME,)),
    StepDef(StepKey.TRIAGE, "Triage", "/triage", needs=(StepKey.IMPORT,)),
    StepDef(StepKey.BUILD_LIST, "Build a list", "/lists", needs=(StepKey.IMPORT,)),
    StepDef(
        StepKey.CAMPAIGN,
        "Create a campaign",
        "/campaigns",
        needs=(StepKey.IMPORT,),
        needs_any=(StepKey.GMAIL, StepKey.CHROME),
    ),
    StepDef(StepKey.ARM_SCHEDULE, "Arm scheduled runs", "/linkedin", needs=(StepKey.FIRST_SYNC,)),
    StepDef(StepKey.LOGIN_ITEM, "Run at login", "/settings", optional=True),
)

_TITLES: Final = {step.key: step.title for step in STEPS}


class UnknownStep(LookupError):
    """No setup step has that key."""


@dataclass(frozen=True, slots=True)
class Step:
    key: StepKey
    title: str
    state: StepState
    reason: str
    """A plain sentence: why the step isn't done, or what shows it is."""
    target: str
    needs: tuple[StepKey, ...] = ()
    needs_any: tuple[StepKey, ...] = ()
    optional: bool = False
    skipped: bool = False
    """The person skipped it, whatever ``state`` says."""
    next: bool = False


@dataclass(frozen=True, slots=True)
class Readings:
    """What the steps read that is not in the database, read by the caller.

    ``chrome_pid`` is the live process holding netkeep's Chrome profile, or None.
    ``login_item`` is None when it could not be read.
    """

    chrome_pid: int | None = None
    login_item: LoginItemStatus | None = None
    login_item_error: str | None = None


@dataclass(frozen=True, slots=True)
class _Raw:
    """A step's own state, before gating and skips: done, in progress or to do."""

    state: StepState
    reason: str


def skipped_keys(session: Session, user: User) -> frozenset[StepKey]:
    """The steps ``user`` skipped. A stored key no step has any more is ignored."""
    stored = get_setting(session, user, SKIPPED_KEY)
    if not isinstance(stored, list):
        return frozenset()
    known = {key.value: key for key in StepKey}
    return frozenset(known[value] for value in stored if isinstance(value, str) and value in known)


def skip(session: Session, user: User, key: str) -> None:
    """Mark step ``key`` skipped for ``user``. Needs a writer session."""
    _store(session, user, skipped_keys(session, user) | {_key(key)})


def unskip(session: Session, user: User, key: str) -> None:
    """Clear the skip on step ``key`` for ``user``. Needs a writer session."""
    _store(session, user, skipped_keys(session, user) - {_key(key)})


def _key(value: str) -> StepKey:
    try:
        return StepKey(value)
    except ValueError:
        raise UnknownStep(f"no setup step {value!r}") from None


def _store(session: Session, user: User, keys: frozenset[StepKey]) -> None:
    set_setting(session, user, SKIPPED_KEY, [key.value for key in StepKey if key in keys])


def steps(session: Session, user: User, readings: Readings) -> list[Step]:
    """Every setup step for ``user``, in order, with its state. Read-only."""
    raw = _raw_states(session, user, readings)
    skipped = skipped_keys(session, user)
    done = {key for key, value in raw.items() if value.state is StepState.DONE}
    out: list[Step] = []
    for definition in STEPS:
        own = raw[definition.key]
        state, reason = own.state, own.reason
        if state is not StepState.DONE:
            missing = _missing(definition, done)
            if missing:
                state = StepState.BLOCKED
                reason = _waits_for(definition, missing)
            elif definition.key in skipped:
                state = StepState.SKIPPED
                reason = f"Skipped. {own.reason}"
        out.append(
            Step(
                key=definition.key,
                title=definition.title,
                state=state,
                reason=reason,
                target=definition.target,
                needs=definition.needs,
                needs_any=definition.needs_any,
                optional=definition.optional,
                skipped=definition.key in skipped,
            )
        )
    return _mark_next(out)


def complete(rows: Sequence[Step]) -> bool:
    """Every step that isn't optional is done or skipped."""
    return all(row.optional or row.state is StepState.DONE or row.skipped for row in rows)


def _missing(definition: StepDef, done: set[StepKey]) -> tuple[StepKey, ...]:
    """The prerequisites of ``definition`` that aren't done; an unmet any-of group whole."""
    missing = tuple(key for key in definition.needs if key not in done)
    if definition.needs_any and not done.intersection(definition.needs_any):
        missing += definition.needs_any
    return missing


def _waits_for(definition: StepDef, missing: Sequence[StepKey]) -> str:
    all_of = [_TITLES[key] for key in missing if key in definition.needs]
    any_of = [_TITLES[key] for key in missing if key not in definition.needs]
    parts = []
    if all_of:
        parts.append(" and ".join(all_of))
    if any_of:
        parts.append("one of " + " or ".join(any_of))
    return f"Waits for {' and '.join(parts)}."


def _mark_next(rows: list[Step]) -> list[Step]:
    first_todo = next((row.key for row in rows if row.state is StepState.TODO), None)
    return [
        replace(row, next=row.state is StepState.IN_PROGRESS or row.key == first_todo)
        for row in rows
    ]


def _raw_states(session: Session, user: User, readings: Readings) -> dict[StepKey, _Raw]:
    stats = contact_stats(session, user)
    return {
        StepKey.ABOUT_YOU: _about_you(session, user),
        StepKey.IMPORT: _import(session, user, stats.total),
        StepKey.GMAIL: _gmail(session, user),
        StepKey.CHROME: _chrome(session, user, readings.chrome_pid),
        StepKey.FIRST_SYNC: _first_sync(session, user),
        StepKey.FIRST_INBOX: _first_inbox(session, user),
        StepKey.TRIAGE: _triage(stats.total, stats.untriaged),
        StepKey.BUILD_LIST: _build_list(session, user),
        StepKey.CAMPAIGN: _campaign(session, user),
        StepKey.ARM_SCHEDULE: _arm_schedule(session, user),
        StepKey.LOGIN_ITEM: _login_item(readings.login_item, readings.login_item_error),
    }


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _about_you(session: Session, user: User) -> _Raw:
    contact = get_self_contact(session, user)
    if contact is not None and contact.first_name.strip():
        return _Raw(StepState.DONE, "Your details are set for test sends.")
    return _Raw(
        StepState.TODO,
        "Add your name and company in Settings, About you: a test send renders with them.",
    )


def _import(session: Session, user: User, total: int) -> _Raw:
    drafts = (
        session.scalar(
            scoped(user, ImportRun)
            .with_only_columns(func.count(ImportRun.id))
            .where(ImportRun.status == ImportStatus.DRAFT)
        )
        or 0
    )
    waiting = f"{_plural(drafts, 'draft import')} waiting to be finished."
    if total:
        # An open draft is a note, not a gate: the contacts are there to work with.
        imported = f"{_plural(total, 'contact')} imported."
        return _Raw(StepState.DONE, f"{imported} {waiting}" if drafts else imported)
    if drafts:
        return _Raw(StepState.IN_PROGRESS, waiting)
    return _Raw(StepState.TODO, "Nothing imported yet: bring in a CSV or your LinkedIn archive.")


def _gmail(session: Session, user: User) -> _Raw:
    """Done once a mailbox is connected (``ok``). Arming it to send is its own decision,
    made on the mailbox card, so it is not part of this step. The Keychain is never read:
    the OAuth client is a secret, and the mailbox rows and the wizard's progress say
    enough."""
    rows = session.scalars(scoped(user, Mailbox)).all()
    if any(row.status is MailboxStatus.OK for row in rows):
        return _Raw(StepState.DONE, "A Gmail mailbox is connected.")
    if any(row.status is MailboxStatus.REAUTH_REQUIRED for row in rows):
        return _Raw(StepState.IN_PROGRESS, "Your mailbox needs signing in again.")
    progress = load_gmail_setup(session, user)
    if progress.done or progress.project_id is not None:
        return _Raw(
            StepState.IN_PROGRESS,
            f"{_plural(len(progress.done), 'Google Cloud step')} marked done in Settings, Gmail.",
        )
    return _Raw(StepState.TODO, "Set up your Google Cloud project, then connect your mailbox.")


def _chrome(session: Session, user: User, chrome_pid: int | None) -> _Raw:
    flag = session_flag(session, user)
    if flag is not None:
        return _Raw(
            StepState.IN_PROGRESS,
            f"LinkedIn answered with a {flag.outcome.value.replace('_', ' ')}; clear the"
            " session flag on the LinkedIn page once the account is healthy.",
        )
    evidence = last_session_evidence(session, user, account_id_for(session, user))
    if evidence is not None and evidence.logged_in:
        return _Raw(StepState.DONE, f"Signed in to LinkedIn, last confirmed by {evidence.source}.")
    running = chrome_pid is not None
    if evidence is not None:
        return _Raw(
            StepState.IN_PROGRESS,
            "The last check found no LinkedIn session: sign in to LinkedIn in netkeep's"
            " Chrome, then click Check connection.",
        )
    if running:
        return _Raw(
            StepState.IN_PROGRESS,
            "netkeep's Chrome is running: sign in to LinkedIn there, then click Check connection.",
        )
    return _Raw(
        StepState.TODO,
        "Start netkeep's Chrome from the LinkedIn page and sign in to LinkedIn there.",
    )


def _first_sync(session: Session, user: User) -> _Raw:
    kind = SyncRunKind.CONNECTIONS_FULL
    if latest_run(session, user, kind, status=SyncRunStatus.COMPLETED) is not None:
        return _Raw(StepState.DONE, "A full connections sync has completed.")
    if latest_run(session, user, kind, status=SyncRunStatus.RUNNING) is not None:
        return _Raw(StepState.IN_PROGRESS, "A full connections sync is running.")
    return _Raw(StepState.TODO, "Start a full sync of your connections on the LinkedIn page.")


def _first_inbox(session: Session, user: User) -> _Raw:
    if has_completed_inbox_poll(session, user):
        return _Raw(StepState.DONE, "An inbox read has completed.")
    if latest_run(session, user, SyncRunKind.INBOX, status=SyncRunStatus.RUNNING) is not None:
        return _Raw(StepState.IN_PROGRESS, "An inbox read is running.")
    return _Raw(
        StepState.TODO,
        "Click Check inbox now on the LinkedIn page: scheduled reads wait for one you start.",
    )


def _triage(total: int, untriaged: int) -> _Raw:
    if total and not untriaged:
        return _Raw(StepState.DONE, f"All {_plural(total, 'contact')} triaged.")
    if total and untriaged < total:
        return _Raw(StepState.IN_PROGRESS, f"{total - untriaged} of {total} triaged.")
    if total:
        return _Raw(StepState.TODO, f"None of {_plural(total, 'contact')} triaged yet.")
    return _Raw(StepState.TODO, "Nothing to triage yet.")


def _build_list(session: Session, user: User) -> _Raw:
    # The built-in "Validated" list is seeded at every start, so it shows nothing built.
    count = (
        session.scalar(
            scoped(user, ContactList)
            .with_only_columns(func.count(ContactList.id))
            .where(ContactList.builtin.is_(False))
        )
        or 0
    )
    if count:
        return _Raw(StepState.DONE, f"{_plural(count, 'list')} built.")
    return _Raw(StepState.TODO, "Build a list of the contacts you want to reach.")


def _campaign(session: Session, user: User) -> _Raw:
    statuses = set(session.scalars(scoped(user, Campaign).with_only_columns(Campaign.status)).all())
    if statuses - {CampaignStatus.DRAFT}:
        return _Raw(StepState.DONE, "A campaign has left draft.")
    if statuses:
        return _Raw(StepState.IN_PROGRESS, "A campaign is in draft: review and start it.")
    return _Raw(StepState.TODO, "Create a campaign to reach a list or a filter.")


def _arm_schedule(session: Session, user: User) -> _Raw:
    if scheduled_runs_armed(session, user, account_id_for(session, user)):
        return _Raw(StepState.DONE, "Scheduled LinkedIn runs are armed.")
    return _Raw(
        StepState.TODO,
        "After a supervised run, arm scheduled runs on the LinkedIn page so they fire on"
        " their own.",
    )


def _login_item(status: LoginItemStatus | None, error: str | None) -> _Raw:
    if status is None:
        return _Raw(StepState.TODO, f"The login item couldn't be read: {error or 'unknown'}.")
    if not status.supported:
        return _Raw(StepState.TODO, status.reason or "Run at login needs macOS.")
    if status.state in (LoginItemState.REGISTERED, LoginItemState.RUNNING):
        return _Raw(StepState.DONE, f"netkeep starts at login ({status.state.value}).")
    if status.state is LoginItemState.DIFFERENT_NETKEEP:
        return _Raw(
            StepState.TODO,
            f"{status.state.value}: click Repair in Settings, Run at login, so it starts this one.",
        )
    if status.state is LoginItemState.NEEDS_REPAIR:
        return _Raw(
            StepState.TODO,
            f"{status.state.value}: an old install script's agent can't start; click Repair in"
            " Settings, Run at login.",
        )
    return _Raw(StepState.TODO, "Turn on Run at login in Settings so netkeep starts with you.")
