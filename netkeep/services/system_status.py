"""The three status chips' server-side parts, rolled up (#761, ADR 0014 rule 1).

The top bar's chips (#751), Gmail, LinkedIn and netkeep, and the menu bar read one
roll-up from here, so no second copy of the rules can drift. Each chip rolls up its
parts: every part has a tone on one scale, the chip takes the worst, and its word and
fact come from the part that set it. A read that failed is a part of its own that says
so, never a guess.

What only a client can know stays with it, and it adds those parts by the same rule
(:func:`roll_up`): whether the server answers, and, for the UI, live updates. The
"Unknown" rule for Gmail and LinkedIn while the server is down is a client's too.

**Times are not text here.** A fact or detail that says when (``checked 3 min ago``,
``next run 14:20``) holds a ``{name}`` placeholder, and the chip's ``times`` gives each
name an absolute UTC time and a style. Each client formats it when it renders, so a
cached answer never shows a stale "2 min ago" (maintainer, 2026-10-22).

Read-only and per-user: every read is a database read or this process's memory.
Nothing here touches the browser or Gmail.
"""

# The words are the UI's, typographic apostrophes and the Settings arrow included.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import enum
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy.orm import Session

from netkeep.config import Settings
from netkeep.models import MailboxArm, MailboxStatus, SyncRunKind, SyncRunTrigger, User
from netkeep.services import attention as attention_service
from netkeep.services import budgets, runs
from netkeep.services import mailboxes as mailbox_service
from netkeep.services import poll_status as poll_service
from netkeep.services import posture as posture_service
from netkeep.services.linkedin_accounts import (
    account_id_for,
    find_account,
    schedule_pause_state,
    scheduled_runs_armed,
)
from netkeep.services.linkedin_session import session_flag
from netkeep.services.scheduler import SERVED_SCHEDULES, stored_due

log = logging.getLogger(__name__)

Tone = Literal["ok", "off", "busy", "warn", "bad"]

TONE_RANK: Final[Mapping[Tone, int]] = {"ok": 0, "off": 1, "busy": 2, "warn": 3, "bad": 4}
"""Worst wins, in this order. Off outranks ok: a chip with a part that is off or not
set up is not working in full, so it says so rather than showing green."""

SystemKey = Literal["gmail", "linkedin", "netkeep"]
Page = Literal["/", "/gmail", "/linkedin", "/settings"]


class TimeStyle(enum.StrEnum):
    """How a client writes a time it fills into a placeholder."""

    AGO = "ago"
    """Elapsed: "just now", "3 min ago", "2 h ago"."""
    NEXT = "next"
    """Ahead: "in 7 min", "14:20", "Tue 14:20"."""
    WHEN = "when"
    """A date and time, in the reader's locale."""


@dataclass(frozen=True, slots=True)
class Time:
    at: datetime
    style: TimeStyle


@dataclass(frozen=True, slots=True)
class Target:
    """Where a chip or a part sends you: a page, and the section on it."""

    to: Page
    hash: str | None = None


@dataclass(frozen=True, slots=True)
class Part:
    key: str
    label: str
    """The row's name in the popover."""
    tone: Tone
    word: str
    """What the chip says when this part sets its tone."""
    detail: str
    """The row's detail in the popover."""
    fact: str | None = None
    """The chip's muted fact when this part sets it; the chip's own fact when None."""
    target: Target | None = None
    """Where this part is fixed, when it sets the chip's tone."""


@dataclass(frozen=True, slots=True)
class Chip:
    key: SystemKey
    name: str
    tone: Tone
    word: str
    fact: str
    parts: tuple[Part, ...]
    target: Target
    page: str
    """The target page's name, for the popover's button."""
    times: Mapping[str, Time] = field(default_factory=dict)
    """Every ``{name}`` in the chip's fact and its parts' facts and details."""


@dataclass(frozen=True, slots=True)
class RunInProgress:
    id: int
    kind: SyncRunKind
    label: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class SystemStatus:
    gmail: Chip
    linkedin: Chip
    netkeep: Chip
    linkedin_paused: bool
    """LinkedIn scheduled runs are paused: the menu bar offers Resume, not Pause."""
    linkedin_run: RunInProgress | None
    """The LinkedIn run in progress, which the Update and Quit confirmations name."""
    campaign_sending: bool
    """A campaign tick is in flight in this process and may be sending (ADR 0014 rule 5)."""


GMAIL_SETTINGS: Final = Target("/settings", "gmail")
SUPPORT: Final = Target("/settings", "support")

PAGE_NAMES: Final[Mapping[str, str]] = {
    "/": "Dashboard",
    "/gmail": "Gmail",
    "/linkedin": "LinkedIn",
    "/settings": "Settings",
    "/settings#support": "Settings › Support",
}


def page_name(target: Target) -> str:
    return PAGE_NAMES.get(f"{target.to}#{target.hash or ''}") or PAGE_NAMES[target.to]


def lead_part(parts: Sequence[Part], lead: Sequence[str] = ()) -> Part | None:
    """The part that sets the chip: the worst tone, and among parts with that tone the
    first in ``lead`` order (then the popover's order), so the most urgent of two
    problems speaks. A chip that is all well says its ``calm`` part instead."""
    if not parts:
        return None

    def order(index: int, part: Part) -> int:
        return lead.index(part.key) if part.key in lead else len(lead) + index

    ranked = sorted(enumerate(parts), key=lambda pair: (-TONE_RANK[pair[1].tone], order(*pair)))
    return ranked[0][1]


def roll_up(
    system: SystemKey,
    name: str,
    parts: Sequence[Part],
    *,
    lead: Sequence[str],
    fact: str,
    target: Target,
    calm: str | None = None,
    times: Mapping[str, Time] | None = None,
) -> Chip:
    """A chip from its parts. A client adding its own parts calls this again with them,
    the chip's ``times``, and the same ``lead`` and ``calm``."""
    worst = lead_part(parts, lead)
    top = worst
    if worst is not None and worst.tone == "ok":
        top = next((part for part in parts if part.key == calm), worst)
    to = target if top is None or top.target is None else top.target
    return Chip(
        key=system,
        name=name,
        tone="off" if top is None else top.tone,
        word="Unknown" if top is None else top.word,
        fact=fact if top is None or top.fact is None else top.fact,
        parts=tuple(parts),
        target=to,
        page=page_name(to),
        times=dict(times or {}),
    )


# --- reads ------------------------------------------------------------------------------


class Failed:
    """A read that raised: its part says it couldn't be read."""

    def __repr__(self) -> str:
        return "FAILED"


FAILED: Final = Failed()


@dataclass(frozen=True, slots=True)
class MailboxRead:
    email: str
    status: MailboxStatus
    arm: MailboxArm | None
    status_reason: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RunRead:
    id: int
    kind: SyncRunKind
    trigger: SyncRunTrigger
    started_at: datetime
    progress: Mapping[str, Any] | None = None
    planned: int | None = None
    completed: int | None = None


@dataclass(frozen=True, slots=True)
class LinkedInRead:
    """``/linkedin/status``'s part of it: the session flag and the running run."""

    session_flag: str | None
    running: RunRead | None = None


@dataclass(frozen=True, slots=True)
class ScheduleRead:
    armed: bool
    paused: bool
    scheduler_running: bool
    next_due: datetime | None = None


@dataclass(frozen=True, slots=True)
class PrefillsRead:
    limit: int
    remaining: int


@dataclass(frozen=True, slots=True)
class HeatRead:
    tripped: bool
    resumes_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Reads:
    """Everything the three chips read, each read on its own: one that failed fails
    only its own part. None for prefills means there is no budget row for them."""

    mailboxes: list[MailboxRead] | Failed
    poll: poll_service.PollStatus | Failed
    attention: attention_service.Attention | Failed
    linkedin: LinkedInRead | Failed
    schedule: ScheduleRead | Failed
    prefills: PrefillsRead | Failed | None
    heat: HeatRead | Failed
    key_persisted: bool = True
    """False when the Keychain refused the local API key (#618)."""
    sending: bool = False
    """A campaign tick sent within :data:`BATCH_SHOWS_FOR` (``campaign.tick``, #547)."""
    progress: Mapping[str, Any] | None = None
    """The latest ``run.progress`` event's data, so the running count moves between
    the run's own saves of its progress."""
    campaign_busy: bool = False


def _guarded[T](what: str, read: Callable[[], T]) -> T | Failed:
    try:
        return read()
    except Exception as exc:
        # Only the type: a message can carry an address or a name.
        log.warning("status roll-up: %s could not be read: %s", what, type(exc).__name__)
        return FAILED


BATCH_SHOWS_FOR: Final = timedelta(seconds=15)
"""How long a send batch shows as running. The event arrives after the batch is sent,
and the next tick is a minute away, so this is a short blink, not a measure of the send."""


@dataclass(frozen=True, slots=True)
class Process:
    """What this process knows that the database doesn't, read from ``app.state``."""

    serving: poll_service.Serving
    scheduler_running: bool
    key_persisted: bool
    last_sent_at: datetime | None = None
    """When this user's last campaign tick that sent at least one message ended."""
    progress: Mapping[str, Any] | None = None
    campaign_busy: bool = False


def read_all(
    session: Session, user: User, *, now: datetime, settings: Settings, process: Process
) -> Reads:
    """Every read, for ``user`` at ``now``. Read-only."""
    linkedin = settings.linkedin

    def mailboxes() -> list[MailboxRead]:
        return [
            MailboxRead(
                email=mailbox.email,
                status=mailbox.status,
                arm=mailbox.arm,
                status_reason=mailbox.status_reason,
                updated_at=mailbox.updated_at,
            )
            for mailbox in mailbox_service.list_mailboxes(session, user)
        ]

    def status() -> LinkedInRead:
        flag = session_flag(session, user)
        account = find_account(session, user)
        running = None if account is None else runs.running_run(session, user, account.id)
        if running is None:
            return LinkedInRead(session_flag=None if flag is None else flag.outcome.value)
        derived = runs.view(running)
        return LinkedInRead(
            session_flag=None if flag is None else flag.outcome.value,
            running=RunRead(
                id=running.id,
                kind=running.kind,
                trigger=running.trigger,
                started_at=running.started_at,
                progress=running.progress_json,
                planned=derived.planned,
                completed=derived.completed,
            ),
        )

    def schedule() -> ScheduleRead:
        account = find_account(session, user)
        account_id = account_id_for(session, user)
        pause = None if account is None else schedule_pause_state(session, user, account.id)
        dues = [stored_due(session, user, account_id, kind) for kind in SERVED_SCHEDULES]
        return ScheduleRead(
            armed=account is not None and scheduled_runs_armed(session, user, account.id),
            paused=pause is not None and pause.paused,
            scheduler_running=process.scheduler_running,
            next_due=min((due for due in dues if due is not None), default=None),
        )

    def prefills() -> PrefillsRead:
        day = budgets.status(
            session,
            user,
            account_id_for(session, user),
            budgets.ActionClass.LI_PREFILLS,
            now=now,
            settings=linkedin.budget,
        ).day
        return PrefillsRead(limit=day.limit, remaining=day.remaining)

    def heat() -> HeatRead:
        account_id = account_id_for(session, user)
        found = posture_service.heat_status(session, user, account_id, now=now, settings=linkedin)
        return HeatRead(tripped=found.tripped, resumes_at=found.resumes_at)

    return Reads(
        mailboxes=_guarded("the mailboxes", mailboxes),
        poll=_guarded(
            "the background checks",
            lambda: poll_service.poll_status(
                session, user, now=now, settings=settings, serving=process.serving
            ),
        ),
        attention=_guarded(
            "attention",
            lambda: attention_service.attention(session, user, now=now, settings=settings),
        ),
        linkedin=_guarded("the LinkedIn status", status),
        schedule=_guarded("the LinkedIn schedule", schedule),
        prefills=_guarded("the prefill budget", prefills),
        heat=_guarded("heat", heat),
        key_persisted=process.key_persisted,
        sending=process.last_sent_at is not None and now - process.last_sent_at < BATCH_SHOWS_FOR,
        progress=process.progress,
        campaign_busy=process.campaign_busy,
    )


# --- shared parts --------------------------------------------------------------------


def _failed(key: str, label: str, target: Target | None = None) -> Part:
    return Part(
        key=key,
        label=label,
        tone="off",
        word="Couldn’t check",
        fact="",
        detail="Couldn’t be read; netkeep tries again shortly",
        target=target,
    )


def _capitalized(text: str) -> str:
    return text[:1].upper() + text[1:]


CHECK_TONE: Final[Mapping[poll_service.CheckState, Tone]] = {
    poll_service.CheckState.SCHEDULED: "ok",
    poll_service.CheckState.DUE: "ok",
    poll_service.CheckState.IDLE: "ok",
    poll_service.CheckState.OUTSIDE_HOURS: "ok",
    poll_service.CheckState.PAUSED: "warn",
    poll_service.CheckState.BLOCKED: "warn",
    poll_service.CheckState.OFF: "off",
    poll_service.CheckState.NOT_RUNNING: "off",
    poll_service.CheckState.NOT_WIRED: "off",
}
"""A background check's tone. A check that waits on its own (outside active hours,
nothing to look at) is working as set up. One that ``netkeep serve`` doesn't run is off
here; the netkeep chip is the one that says serve isn't running."""

STATE_TEXT: Final[Mapping[poll_service.CheckState, str]] = {
    poll_service.CheckState.DUE: "due soon",
    poll_service.CheckState.IDLE: "nothing to check",
    poll_service.CheckState.PAUSED: "paused",
    poll_service.CheckState.OUTSIDE_HOURS: "outside active hours",
    poll_service.CheckState.BLOCKED: "needs attention",
    poll_service.CheckState.OFF: "off",
    poll_service.CheckState.NOT_RUNNING: "not running",
    poll_service.CheckState.NOT_WIRED: "not running yet",
}
"""What a check that has no next time says instead. Never a time."""


def _check_times(prefix: str, check: poll_service.Check) -> dict[str, Time]:
    times: dict[str, Time] = {}
    if check.last_at is not None:
        times[f"{prefix}_last"] = Time(check.last_at, TimeStyle.AGO)
    if check.state is poll_service.CheckState.SCHEDULED and check.next_at is not None:
        times[f"{prefix}_next"] = Time(check.next_at, TimeStyle.NEXT)
    return times


def _check_summary(prefix: str, check: poll_service.Check) -> str:
    """ "checked {last} · next {next}", "next {next}", "checked {last} · paused". Only a
    ``scheduled`` check shows a next time."""
    parts: list[str] = []
    if check.last_at is not None:
        parts.append(f"checked {{{prefix}_last}}")
    if check.state is poll_service.CheckState.SCHEDULED:
        if check.next_at is not None:
            parts.append(f"next {{{prefix}_next}}")
    else:
        parts.append(STATE_TEXT[check.state])
    return " · ".join(parts)


def _last_checked(prefix: str, check: poll_service.Check) -> str:
    return "not checked yet" if check.last_at is None else f"checked {{{prefix}_last}}"


def _check_part(
    key: str,
    label: str,
    prefix: str,
    check: poll_service.Check,
    fact: str,
    target: Target | None = None,
) -> Part:
    summary = _capitalized(_check_summary(prefix, check))
    scheduled = check.state is poll_service.CheckState.SCHEDULED
    return Part(
        key=key,
        label=label,
        tone=CHECK_TONE[check.state],
        word="Checked" if scheduled else _capitalized(STATE_TEXT[check.state]),
        fact=fact,
        detail=f"{summary}. {check.reason}" if check.reason else summary,
        target=target,
    )


def _find_check(
    poll: poll_service.PollStatus | Failed, key: str
) -> poll_service.Check | Failed | None:
    if isinstance(poll, Failed):
        return poll
    return next((check for check in poll.checks if check.key == key), None)


# --- Gmail ---------------------------------------------------------------------------

REASONS: Final[Mapping[str, str]] = {
    "invalid_grant": (
        "Google no longer accepts the token: access was revoked, or the consent screen is"
        " still in Testing and its 7 days are up."
    ),
    "invalid_client": (
        "Google does not know the OAuth client any more: it was deleted, or its secret was reset."
    ),
    "unauthorized_client": (
        "Google will not let the OAuth client use this token. Save a Desktop app client,"
        " then authorize again."
    ),
    "token_missing": "The Keychain has no token for this mailbox.",
    "client_missing": "No OAuth client is stored. Add the client ID and secret first.",
    "disconnected": "Disconnected. Its token is gone from the Keychain.",
    "gmail_api_refused": (
        "The Gmail API refused the token. Enable the Gmail API in the Cloud project"
        " (docs/gmail-setup.md, step 2)."
    ),
    "insufficientPermissions": (
        "The token does not include Gmail access. Authorize again and leave Gmail access"
        " ticked on Google’s page."
    ),
    "http_403": (
        "Gmail refused the token. Authorize again; if it happens again, check the Gmail API"
        " is enabled in the Cloud project (docs/gmail-setup.md, step 2)."
    ),
    "keychain": "The Keychain refused. Unlock it and try again.",
    "unavailable": "Google could not be reached. Try again in a moment.",
}
"""A stored mailbox status reason, for a person: the ones ``reasonText`` in
``frontend/src/features/mailboxes/api.ts`` has that a mailbox row can hold."""


def reason_text(reason: str | None) -> str | None:
    if not reason:
        return None
    return REASONS.get(reason, f'Google answered "{reason}".')


def pick_mailbox(mailboxes: Sequence[MailboxRead]) -> MailboxRead | None:
    """Which mailbox Gmail reads: the one live account (``ok`` or ``reauth_required``)
    when there is one, so a person acting on it sees it first; otherwise the most
    recently touched ``disabled`` row, so a mailbox someone disconnected still reads as
    "disconnected" rather than "not connected". An empty list is the only case with none."""
    live = next(
        (mailbox for mailbox in mailboxes if mailbox.status is not MailboxStatus.DISABLED), None
    )
    if live is not None:
        return live
    return max(mailboxes, key=lambda mailbox: mailbox.updated_at, default=None)


def _mailbox_part(read: list[MailboxRead] | Failed) -> Part:
    if isinstance(read, Failed):
        return _failed("mailbox", "Mailbox")
    mailbox = pick_mailbox(read)
    base = Part(
        key="mailbox", label="Mailbox", tone="ok", word="", detail="", target=GMAIL_SETTINGS
    )
    if mailbox is None:
        return replace(
            base, tone="off", word="Not set up", fact="", detail="No Gmail account is connected"
        )
    if mailbox.status is MailboxStatus.DISABLED:
        return replace(
            base, tone="off", word="Disconnected", fact="", detail=f"{mailbox.email} · disconnected"
        )
    if mailbox.status is MailboxStatus.REAUTH_REQUIRED:
        lines = [
            f"Google signed netkeep out of {mailbox.email}. Nothing is drafted, sent or"
            " checked until you authorize it again.",
            reason_text(mailbox.status_reason),
        ]
        return replace(
            base,
            tone="bad",
            word="Re-authorize",
            fact="nothing sent or checked",
            detail=" ".join(line for line in lines if line is not None),
        )
    return Part(
        key="mailbox",
        label="Mailbox",
        tone="ok",
        word="Connected",
        detail=f"{mailbox.email} · connected",
    )


def _sending_part(mailbox: MailboxRead) -> Part:
    if mailbox.arm is MailboxArm.SEND:
        return Part("sending", "Sending", "ok", "Sending", "Armed to send")
    if mailbox.arm is MailboxArm.DRAFT:
        return Part(
            "sending", "Sending", "ok", "Drafts only", "Armed for drafts only: nothing is sent"
        )
    return Part(
        "sending",
        "Sending",
        "warn",
        "Not armed",
        "Not armed: nothing is drafted or sent",
        target=GMAIL_SETTINGS,
    )


def _flag_part(
    attention: attention_service.Attention | Failed, mailbox: MailboxRead | None
) -> Part | None:
    """``/attention``'s Gmail flag, unless the mailbox part already says it: a draft
    waiting for a contact you shouldn't email (see Leftover drafts), or a mailbox to
    sign in again that the list hasn't caught up with yet."""
    flag = None if isinstance(attention, Failed) else attention.flags.gmail
    if flag is None or (mailbox is not None and mailbox.status is MailboxStatus.REAUTH_REQUIRED):
        return None
    draft = "draft" in flag.lower()
    return Part(
        key="flag",
        label="Leftover drafts" if draft else "Needs you",
        tone="warn",
        word="Draft held" if draft else "Needs you",
        fact="",
        detail=flag,
        target=Target("/gmail", "leftover-drafts") if draft else GMAIL_SETTINGS,
    )


BATCH: Final = Part(
    key="batch",
    label="Campaign batch",
    tone="busy",
    word="Sending now",
    fact="campaign batch",
    detail="A campaign send batch just ran",
)


def gmail_chip(reads: Reads) -> Chip:
    """Gmail: the mailbox (connected, needs re-authorizing or disconnected), whether it
    is armed (drafts only, sending or not armed), the reply check, and a campaign send
    batch while it runs. All well, it says how it sends and when replies were last checked."""
    parts = [_mailbox_part(reads.mailboxes)]
    mailbox = None if isinstance(reads.mailboxes, Failed) else pick_mailbox(reads.mailboxes)
    if mailbox is not None and mailbox.status is MailboxStatus.OK:
        parts.append(_sending_part(mailbox))
    if reads.sending:
        parts.append(BATCH)
    flag = _flag_part(reads.attention, mailbox)
    if flag is not None:
        parts.append(flag)
    fact = ""
    times: dict[str, Time] = {}
    replies = _find_check(reads.poll, poll_service.GMAIL_REPLIES)
    if isinstance(replies, Failed):
        parts.append(_failed("replies", "Reply check"))
    elif replies is not None:
        fact = _last_checked("replies", replies)
        times |= _check_times("replies", replies)
        parts.append(_check_part("replies", "Reply check", "replies", replies, fact))
    return roll_up(
        "gmail",
        "Gmail",
        parts,
        lead=("mailbox", "flag", "batch", "sending", "replies"),
        calm="sending",
        fact=fact,
        target=Target("/gmail"),
        times=times,
    )


# --- LinkedIn ------------------------------------------------------------------------

RUN_WORDS: Final[Mapping[SyncRunKind, str]] = {
    SyncRunKind.CONNECTIONS_FULL: "Full sync",
    SyncRunKind.CONNECTIONS_INCREMENTAL: "Sync",
    SyncRunKind.ENRICH: "Enrichment",
    SyncRunKind.INBOX: "Inbox check",
    SyncRunKind.MESSAGE_SEND: "Prefill",
    SyncRunKind.SESSION_CHECK: "Connection check",
}

RUN_KIND_LABELS: Final[Mapping[SyncRunKind, str]] = {
    SyncRunKind.CONNECTIONS_FULL: "Full connections sync",
    SyncRunKind.CONNECTIONS_INCREMENTAL: "Incremental connections sync",
    SyncRunKind.ENRICH: "Enrichment",
    SyncRunKind.INBOX: "Inbox poll",
    SyncRunKind.MESSAGE_SEND: "LinkedIn prefill",
    SyncRunKind.SESSION_CHECK: "Connection check",
}
"""What a run is called, as the LinkedIn page's ``RUN_KIND_LABELS`` says it."""


def _auto_send(run: RunRead) -> bool:
    """A scheduled ``message_send`` is an auto-send (ADR 0008), not a prefill."""
    return run.kind is SyncRunKind.MESSAGE_SEND and run.trigger is SyncRunTrigger.SCHEDULED


def run_label(run: RunRead) -> str:
    return "LinkedIn auto-send" if _auto_send(run) else RUN_KIND_LABELS[run.kind]


def _linkedin(hash: str) -> Target:
    return Target("/linkedin", hash)


def _session_part(read: LinkedInRead | Failed) -> Part:
    """ "No flag" is not "the session is healthy": nothing here checks the live session
    (a request handler never touches the browser), so the part says only what it knows."""
    target = _linkedin("session")
    if isinstance(read, Failed):
        return _failed("session", "Session", target)
    if read.session_flag == "checkpoint":
        return Part(
            "session",
            "Session",
            "bad",
            "Checkpoint",
            "LinkedIn asked for a checkpoint. No run touches the browser until it is resolved"
            " and the flag cleared.",
            fact="runs stopped",
            target=target,
        )
    if read.session_flag is not None:
        return Part(
            "session",
            "Session",
            "bad",
            "Logged out",
            "Logged out. No run touches the browser until you log in and run netkeep preflight.",
            fact="runs stopped",
            target=target,
        )
    return Part(
        "session",
        "Session",
        "ok",
        "No session flag",
        "No session flag: no run has found LinkedIn logged out",
        target=target,
    )


def _count(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def progress_text(progress: Mapping[str, Any] | None) -> str | None:
    """How far a run got, as "340 of 626", from the two known progress shapes (spec
    9.10's ``ProgressEvent``): an enrichment's ``visited`` of ``planned``, a sync's
    ``connections`` of LinkedIn's ``total``. Anything else has no count to show."""
    if progress is None:
        return None
    for done, of in (("visited", "planned"), ("connections", "total")):
        a, b = _count(progress.get(done)), _count(progress.get(of))
        if a is not None and b is not None and b > 0:
            return f"{_number(a)} of {_number(b)}"
    return None


def _running_part(run: RunRead, live: Mapping[str, Any] | None) -> Part:
    word = "Auto-send" if _auto_send(run) else RUN_WORDS[run.kind]
    own_live = live if live is not None and live.get("run_id") == run.id else None
    progress = progress_text(own_live) or progress_text(run.progress)
    if progress is None and run.planned is not None:
        progress = f"{run.completed or 0} of {run.planned}"
    started = "started {run_started}"
    return Part(
        key="running",
        label="Running",
        tone="busy",
        word=f"{word} running",
        fact=progress or started,
        detail=" · ".join(text for text in (run_label(run), progress, started) if text),
        target=_linkedin("runs"),
    )


def _held_part(attention: attention_service.Attention | Failed) -> Part | None:
    held = None if isinstance(attention, Failed) else attention.flags.dashboard
    if held is None:
        return None
    # Resume is with the LinkedIn steps, under Needs you on the dashboard.
    return Part(
        "held",
        "Auto-send",
        "warn",
        "Auto-send held",
        held,
        fact="",
        target=Target("/", "needs-you"),
    )


def _breaker_part(attention: attention_service.Attention | Failed) -> Part | None:
    """A tripped breaker (route changed, answer lost, Contact info, the inbox poll's):
    scheduled runs of its kind can't start until you reset it on Settings › Posture."""
    breaker = None if isinstance(attention, Failed) else attention.flags.settings
    if breaker is None:
        return None
    return Part(
        "breaker",
        "Breaker",
        "bad",
        "Breaker tripped",
        breaker,
        fact="some runs can’t start",
        target=Target("/settings", "posture"),
    )


def _blocked_part(
    attention: attention_service.Attention | Failed, parts: Sequence[Part]
) -> Part | None:
    """``/attention`` says LinkedIn is blocked (``stopped``) but no other part says why:
    a cause added later, or one only it reads. The chip then never reads green while the
    UI's live region says "blocked"."""
    if isinstance(attention, Failed):
        return None
    if attention.activity.state is not attention_service.ActivityState.STOPPED:
        return None
    if any(TONE_RANK[part.tone] >= TONE_RANK["warn"] for part in parts):
        return None
    return Part(
        "blocked",
        "Blocked",
        "warn",
        "Blocked",
        attention.activity.label or "Something blocks LinkedIn runs from starting",
        fact="",
    )


def _schedule_part(read: ScheduleRead | Failed) -> Part:
    target = _linkedin("schedule")
    if isinstance(read, Failed):
        return _failed("schedule", "Scheduled runs", target)
    base = Part("schedule", "Scheduled runs", "ok", "", "", target=target)
    if read.paused:
        return replace(
            base,
            tone="warn",
            word="Paused",
            detail="Paused: no scheduled run starts until you resume",
        )
    if not read.armed:
        return replace(
            base, tone="off", word="Off", fact="", detail="Off: nothing runs on a schedule"
        )
    if not read.scheduler_running:
        return replace(
            base,
            tone="bad",
            word="No scheduler",
            fact="runs don’t start",
            detail="Armed, but no scheduler is running, so nothing starts on time",
        )
    detail = "Armed" if read.next_due is None else "Armed · next run {schedule_next}"
    return replace(base, word="Armed", detail=detail)


def _prefills_part(read: PrefillsRead | Failed | None) -> Part | None:
    target = _linkedin("budget")
    if isinstance(read, Failed):
        return _failed("prefills", "Prefills today", target)
    if read is None:
        return None
    left = max(read.remaining, 0)
    text = f"{left} of {read.limit} left"
    if left == 0:
        return Part(
            "prefills",
            "Prefills today",
            "warn",
            "No prefills left",
            text,
            fact="until tomorrow",
            target=target,
        )
    return Part("prefills", "Prefills today", "ok", f"{left} prefills left", text, target=target)


def _heat_part(read: HeatRead | Failed) -> Part:
    target = _linkedin("heat")
    if isinstance(read, Failed):
        return _failed("heat", "Heat", target)
    if not read.tripped:
        return Part("heat", "Heat", "ok", "Heat normal", "Normal", target=target)
    until = "" if read.resumes_at is None else " until {heat_resumes}"
    return Part(
        "heat",
        "Heat",
        "bad",
        "Heat high",
        (
            "Over its threshold: scheduled runs are skipped until it cools"
            if read.resumes_at is None
            else "Over its threshold: scheduled runs are skipped until {heat_resumes}"
        ),
        fact=f"runs wait{until}",
        target=target,
    )


def linkedin_chip(reads: Reads) -> Chip:
    """LinkedIn: the session (signed in, logged out or a checkpoint), the running run
    with its progress, scheduled runs (armed, paused, off or no scheduler), the inbox
    check, prefills left today, heat, a tripped breaker and a held auto-send. All well,
    it says scheduled runs are armed and when the inbox was last checked."""
    parts: list[Part | None] = [_session_part(reads.linkedin)]
    times: dict[str, Time] = {}
    running = None if isinstance(reads.linkedin, Failed) else reads.linkedin.running
    if running is not None:
        parts.append(_running_part(running, reads.progress))
        times["run_started"] = Time(running.started_at, TimeStyle.AGO)
    parts.append(_schedule_part(reads.schedule))
    if not isinstance(reads.schedule, Failed) and reads.schedule.next_due is not None:
        times["schedule_next"] = Time(reads.schedule.next_due, TimeStyle.NEXT)
    fact = ""
    inbox = _find_check(reads.poll, poll_service.LINKEDIN_INBOX)
    if isinstance(inbox, Failed):
        parts.append(_failed("inbox", "Inbox check", _linkedin("inbox")))
    elif inbox is not None:
        last = _last_checked("inbox", inbox).removeprefix("checked ")
        fact = f"inbox {last}"
        times |= _check_times("inbox", inbox)
        parts.append(_check_part("inbox", "Inbox check", "inbox", inbox, fact, _linkedin("inbox")))
    parts += [
        _prefills_part(reads.prefills),
        _heat_part(reads.heat),
        _breaker_part(reads.attention),
        _held_part(reads.attention),
    ]
    if not isinstance(reads.heat, Failed) and reads.heat.resumes_at is not None:
        times["heat_resumes"] = Time(reads.heat.resumes_at, TimeStyle.WHEN)
    known = [part for part in parts if part is not None]
    blocked = _blocked_part(reads.attention, known)
    if blocked is not None:
        known.append(blocked)
    return roll_up(
        "linkedin",
        "LinkedIn",
        known,
        lead=LINKEDIN_LEAD,
        calm="schedule",
        fact=fact,
        target=Target("/linkedin"),
        times=times,
    )


LINKEDIN_LEAD: Final = (
    "session",
    "heat",
    "breaker",
    "blocked",
    "held",
    "running",
    "schedule",
    "inbox",
    "prefills",
)

# --- netkeep -------------------------------------------------------------------------

NETKEEP_LEAD: Final = ("server", "keychain", "checks", "live")
"""With the clients' parts: ``server`` (reachable) and ``live`` (the UI's event stream)."""
NETKEEP_CALM: Final = "server"


def netkeep_parts(reads: Reads) -> list[Part]:
    """netkeep's server-side parts: background checks (``netkeep serve``), and the
    Keychain when it refused the key. A client adds whether it reaches the server."""
    parts: list[Part] = []
    if isinstance(reads.poll, Failed):
        parts.append(_failed("checks", "Background checks"))
    elif reads.poll.background_running:
        parts.append(
            Part(
                "checks",
                "Background checks",
                "ok",
                "Connected",
                "netkeep serve is running them",
            )
        )
    else:
        parts.append(
            Part(
                "checks",
                "Background checks",
                "warn",
                "No background checks",
                "netkeep serve isn’t running, so nothing is checked in the background",
                fact="serve isn’t running",
            )
        )
    if not reads.key_persisted:
        parts.append(
            Part(
                "keychain",
                "Keychain",
                "warn",
                "Keychain refused",
                "Refused · the local API key lives in memory until netkeep stops",
                fact="key in memory",
            )
        )
    return parts


def netkeep_chip(reads: Reads) -> Chip:
    """netkeep with only the server's parts; each client rolls it up again with its own
    (:data:`NETKEEP_LEAD`, :data:`NETKEEP_CALM`)."""
    return roll_up(
        "netkeep",
        "netkeep",
        netkeep_parts(reads),
        lead=NETKEEP_LEAD,
        calm=NETKEEP_CALM,
        fact="",
        target=SUPPORT,
    )


# --- all of it -----------------------------------------------------------------------


def system_status(reads: Reads) -> SystemStatus:
    """The three chips, and what the menu bar's actions need besides. Pure."""
    running = None if isinstance(reads.linkedin, Failed) else reads.linkedin.running
    return SystemStatus(
        gmail=gmail_chip(reads),
        linkedin=linkedin_chip(reads),
        netkeep=netkeep_chip(reads),
        linkedin_paused=not isinstance(reads.schedule, Failed) and reads.schedule.paused,
        linkedin_run=None
        if running is None
        else RunInProgress(
            id=running.id,
            kind=running.kind,
            label=run_label(running),
            started_at=running.started_at,
        ),
        campaign_sending=reads.campaign_busy,
    )
