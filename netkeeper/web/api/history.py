"""``/history``: the old tool's history, from the UI (#601).

``POST /history/import`` and ``POST /history/scan-gmail`` run the same services as
``netkeeper history import`` and ``history scan-gmail`` (docs/history-import.md), and
answer with the same report. Both are dry runs unless ``apply`` is set: they do every
step, report it, and roll the database back. Neither has an undo, because the CLI has
none: an import adds nothing twice, so running it again is the way to correct it.

The Gmail scan is read-only (ADR 0009): it searches and reads headers, and the only
Gmail calls it makes are the reads in :func:`netkeeper.services.history_scan.read_gmail`.
It reads Gmail with no database session open, then applies the result in one writer
transaction, like the CLI. Tests set ``app.state.gmail_opener`` to a fake.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.gmail import Gmail, GmailError
from netkeeper.crm.history import ImportReport, import_workbook
from netkeeper.crm.history_workbook import WorkbookError, read_workbook
from netkeeper.db import session_scope
from netkeeper.models import HistoryReplyKind
from netkeeper.services import history_scan
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.web.deps import CurrentUser, SessionDep, read_only

log = logging.getLogger(__name__)

router = APIRouter(tags=["history"])

MAX_UPLOAD_BYTES: Final = 50 * 1024 * 1024
"""A workbook of one tab per campaign is far smaller; this refuses a wrong file early."""

GmailOpener = Callable[[int, int], Gmail]

Responses = dict[int | str, dict[str, Any]]
INVALID: Responses = {422: {"description": "A file that is not an .xlsx workbook"}}
GMAIL: Responses = {
    409: {"description": "No such mailbox, or none is connected"},
    502: {"description": "Gmail refused the first call"},
}


class HistoryCampaignOut(BaseModel):
    """What the import did, or would do, for one campaign tab."""

    model_config = ConfigDict(extra="forbid")

    tab: int = Field(description="The tab's position in the workbook, from 1.")
    name: str
    started_on: date
    last_batch_on: date | None
    listed: int
    matched: int
    unmatched: int
    ambiguous: int
    created: int
    clicks: int
    opens: int | None
    bounces: int | None
    sent_to: int | None
    unlisted: int | None
    new_rows: int
    new_interactions: int
    warnings: list[str]


class HistorySkippedTabOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tab: int
    reason: str


class HistoryImportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool = Field(description="False for a dry run: nothing was written.")
    sha256: str
    campaigns: list[HistoryCampaignOut]
    skipped: list[HistorySkippedTabOut]
    new_campaigns: int
    new_rows: int
    new_interactions: int
    unmatched: list[str]
    ambiguous: list[str]
    unlisted: int = Field(description="Recipients the workbook does not name, across all tabs.")


class HistoryScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mailbox: str | None = Field(
        default=None, description="Mailbox address or ID; the connected one if omitted."
    )
    apply: bool = False
    rescan: bool = Field(default=False, description="Scan recipients an earlier apply scanned.")
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Scan at most this many recipients, and skip the subject search.",
    )


class HistoryScanCampaignOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign: str
    replies: int
    unsubscribes: int
    auto: int
    bounces: int
    by_subject: int
    by_subject_unsubscribe: int


class HistoryScanSampleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: HistoryReplyKind
    addresses: list[str]


class HistoryScanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    nothing_to_scan: bool = Field(
        description="No imported recipients or campaign subjects were left to scan."
    )
    scanned: int
    nothing_found: int
    campaigns: list[HistoryScanCampaignOut]
    samples: list[HistoryScanSampleOut]
    subject_samples: list[str]
    subject_capped: list[str] = Field(description="Campaigns whose subject search hit its limit.")
    subject_unsearchable: list[str]
    no_subject: list[str] = Field(description="Campaigns with no subject to search for.")
    subject_search_skipped: bool = Field(
        description="True when a limit skipped the subject search."
    )
    flagged: int
    opted_out: int
    new_interactions: int
    matched_late: int
    unconfirmed_notices: int
    no_contact: list[str]
    stopped: str | None = Field(
        description="Why Gmail stopped the scan early, if it did; run it again to continue."
    )
    remaining: int = Field(description="Recipients not reached when the scan stopped.")
    subject_remaining: int


def import_out(report: ImportReport, *, applied: bool) -> HistoryImportOut:
    return HistoryImportOut(
        applied=applied,
        sha256=report.sha256,
        campaigns=[
            HistoryCampaignOut(
                tab=c.index + 1,
                name=c.name,
                started_on=c.started_on,
                last_batch_on=c.last_batch_on,
                listed=c.listed,
                matched=c.matched,
                unmatched=c.unmatched,
                ambiguous=c.ambiguous,
                created=c.created,
                clicks=c.clicked,
                opens=c.opens_count,
                bounces=c.bounces_count,
                sent_to=c.recipients_count,
                unlisted=c.unlisted,
                new_rows=c.new_rows,
                new_interactions=c.new_interactions,
                warnings=list(c.warnings),
            )
            for c in report.campaigns
        ],
        skipped=[HistorySkippedTabOut(tab=s.index + 1, reason=s.reason) for s in report.skipped],
        new_campaigns=report.new_campaigns,
        new_rows=sum(c.new_rows for c in report.campaigns),
        new_interactions=sum(c.new_interactions for c in report.campaigns),
        unmatched=list(report.unmatched),
        ambiguous=list(report.ambiguous),
        unlisted=report.unlisted,
    )


@router.post("/history/import", operation_id="import_history", responses=INVALID)
def import_history(
    user: CurrentUser,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="The old tool's workbook, as .xlsx.")],
    apply: Annotated[bool, Query(description="Write; without it, a dry run.")] = False,
    create_missing: Annotated[
        bool,
        Query(description="Create a contact (name and address only) for each unknown address."),
    ] = False,
) -> HistoryImportOut:
    """Import the old tool's campaigns and the people its workbook names, as
    ``netkeeper history import`` does. A dry run rolls the transaction back."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="the workbook is over the 50 MB limit")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="the workbook is over the 50 MB limit")
    try:
        workbook = read_workbook(data)
    except WorkbookError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report = import_workbook(session, user, workbook, create_missing=create_missing)
    if not apply:
        session.rollback()
    return import_out(report, applied=apply)


def scan_out(
    report: history_scan.ScanReport,
    *,
    applied: bool,
    limited: bool,
    no_subject: list[str],
) -> HistoryScanOut:
    none = history_scan.KindCount()
    names = report.campaign_names
    campaign_ids = sorted(set(report.by_campaign) | set(report.by_subject))
    return HistoryScanOut(
        applied=applied,
        nothing_to_scan=False,
        scanned=report.scanned,
        nothing_found=report.nothing,
        campaigns=[
            HistoryScanCampaignOut(
                campaign=names.get(i, str(i)),
                replies=report.by_campaign.get(i, none).reply,
                unsubscribes=report.by_campaign.get(i, none).unsubscribe,
                auto=report.by_campaign.get(i, none).auto,
                bounces=report.by_campaign.get(i, none).bounce,
                by_subject=report.by_subject.get(i, 0),
                by_subject_unsubscribe=report.by_subject_unsubscribe.get(i, 0),
            )
            for i in campaign_ids
        ],
        samples=[
            HistoryScanSampleOut(kind=kind, addresses=list(addresses))
            for kind in HistoryReplyKind
            if (addresses := report.samples.get(kind))
        ],
        subject_samples=list(report.subject_samples),
        subject_capped=[names.get(i, str(i)) for i in report.subject_capped],
        subject_unsearchable=[names.get(i, str(i)) for i in report.subject_unsearchable],
        no_subject=no_subject,
        subject_search_skipped=limited,
        flagged=report.flagged,
        opted_out=report.opted_out,
        new_interactions=report.new_interactions,
        matched_late=report.matched_late,
        unconfirmed_notices=report.unconfirmed_notices,
        no_contact=list(report.no_contact),
        stopped=report.stopped,
        remaining=report.remaining,
        subject_remaining=report.subject_remaining,
    )


def empty_scan(*, applied: bool, no_subject: list[str]) -> HistoryScanOut:
    return scan_out(
        history_scan.ScanReport(), applied=applied, limited=False, no_subject=no_subject
    ).model_copy(update={"nothing_to_scan": True})


@router.post("/history/scan-gmail", operation_id="scan_history_gmail", responses=GMAIL)
@read_only  # reads Gmail with no session open, then opens its own writer
def scan_history_gmail(body: HistoryScanIn, request: Request, user: CurrentUser) -> HistoryScanOut:
    """Search Gmail, read-only, for what the imported recipients sent back, as
    ``netkeeper history scan-gmail`` does. A scan Gmail stops saves what it finished and
    answers with ``stopped``; run it again to continue."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with session_scope(factory) as session:
        wanted = None if body.mailbox is None else body.mailbox.strip().lower()
        if wanted is None:
            box = mailbox_service.live_mailbox(session, user)
        else:
            box = next(
                (
                    m
                    for m in mailbox_service.list_mailboxes(session, user)
                    if m.email == wanted or str(m.id) == wanted
                ),
                None,
            )
        if box is None:
            raise HTTPException(
                status_code=409,
                detail="no mailbox is connected" if wanted is None else f"no mailbox {wanted}",
            )
        mailbox_id = box.id
        targets = history_scan.scan_targets(session, user, rescan=body.rescan, limit=body.limit)
        # A trial run on a few recipients skips the subject search, and so marks no
        # campaign as searched by subject.
        subjects = (
            []
            if body.limit is not None
            else history_scan.subject_targets(session, user, rescan=body.rescan)
        )
        no_subject = history_scan.campaigns_without_subject(session, user)
    if not targets and not subjects:
        return empty_scan(applied=body.apply, no_subject=no_subject)
    try:
        gmail = _open_gmail(request)(user.id, mailbox_id)
    except (mailbox_service.MailboxNotFound, mailbox_service.MailboxNotReady) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GmailError as exc:
        log.info("history scan could not open Gmail: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"Gmail: {exc}") from exc
    read = history_scan.read_gmail(gmail, targets, subjects)
    with session_scope(factory, write=True) as session:
        report = history_scan.apply_scan(session, user, read, now=datetime.now(UTC))
        if not body.apply:
            session.rollback()
    return scan_out(
        report, applied=body.apply, limited=body.limit is not None, no_subject=no_subject
    )


def _open_gmail(request: Request) -> GmailOpener:
    """The Gmail client for a mailbox. Tests set ``app.state.gmail_opener`` to a fake."""
    opener: GmailOpener | None = getattr(request.app.state, "gmail_opener", None)
    if opener is not None:
        return opener
    factory: sessionmaker[Session] = request.app.state.session_factory
    endpoints = getattr(request.app.state, "gmail_endpoints", None)
    return lambda user_id, mailbox_id: mailbox_service.open_gmail(
        factory, user_id, mailbox_id, endpoints=endpoints
    )
