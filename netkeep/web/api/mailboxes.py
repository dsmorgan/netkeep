"""``/mailboxes``: the Gmail account campaigns send from, and its OAuth flow (spec 11.5; P3-01).

The flow, from the Settings page:

1. ``PUT /mailboxes/oauth/client`` stores the Desktop-app client (Keychain).
2. ``POST /mailboxes/oauth/start`` answers the Google URL to open. Its redirect
   is this server's ``/mailboxes/oauth/callback`` on the host the page used, a
   loopback address (the CSRF middleware refuses any other ``Host``).
3. Google redirects the browser to the callback, the one route a browser
   navigation reaches without ``X-Netkeep-Client`` (spec 14.2). It checks
   ``state`` against an authorization this user started in this process,
   exchanges the code, asks Gmail whose account it is, stores the token, and
   redirects to ``/settings?gmail=connected`` (or ``gmail=error&reason=<code>``), or to
   the page named by ``return_to`` at start (#656), when it is one of the app's own pages.

Arming (#277): ``POST /mailboxes/{id}/arm`` with ``draft``, then, as a separate
step, ``send``, which answers ``409`` until a draft made there has been found by
its Message-ID; ``POST /mailboxes/{id}/disarm`` undoes both. Like every state
change, both need the CSRF header.

No handler holds a session while it talks to Google. The access log never shows
the callback's query string (``netkeep.logging_setup``), which carries the code.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode

from anyio import from_thread
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from netkeep.campaigns import gmail_oauth
from netkeep.db import session_scope
from netkeep.logging_setup import personal_exc
from netkeep.models import Mailbox, MailboxStatus, User
from netkeep.models.base import utcnow
from netkeep.services import keychain, message_id_check
from netkeep.services import mailboxes as service
from netkeep.services.campaign_sender import GmailSender
from netkeep.services.events import Event, EventBus
from netkeep.web.deps import AuthProvider, CurrentUser, SessionDep, effective_settings, read_only
from netkeep.web.schemas import (
    MailboxArmIn,
    MailboxArmStepsOut,
    MailboxOut,
    MailboxStatusOut,
    OAuthClientIn,
    OAuthStartIn,
    OAuthStartOut,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["mailboxes"])

CALLBACK_ROUTE = "mailbox_oauth_callback"
SETTINGS_PATH = service.DEFAULT_RETURN_TO
OUTCOME_KEYS = frozenset({"gmail", "reason"})

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such mailbox for this user"}}
NO_CLIENT: Responses = {409: {"description": "No OAuth client is stored yet"}}
INVALID: Responses = {422: {"description": "Not a usable Desktop-app OAuth client"}}
NO_KEYCHAIN: Responses = {503: {"description": "The Keychain refused"}}
NOT_ARMABLE: Responses = {409: {"description": "Not armable this way yet; the detail says why"}}


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except service.MailboxNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.ArmRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.ClientNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except gmail_oauth.ClientConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except keychain.KeychainUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _endpoints(request: Request) -> gmail_oauth.GoogleEndpoints | None:
    endpoints: gmail_oauth.GoogleEndpoints | None = request.app.state.gmail_endpoints
    return endpoints


def _pending(request: Request) -> service.PendingAuthorizations:
    pending: service.PendingAuthorizations = request.app.state.pending_oauth
    return pending


def _publish(request: Request, event: Event) -> None:
    """Publish from a handler running on the thread pool (the bus lives on the loop)."""
    bus: EventBus = request.app.state.bus
    from_thread.run_sync(bus.publish, event)


def _gmail_sender(request: Request) -> GmailSender | None:
    """The running campaign engine's Gmail sender; None outside ``serve``."""
    engine = request.app.state.campaign_engine
    sender = None if engine is None else engine.sender
    return sender if isinstance(sender, GmailSender) else None


def _status_event(mailbox: Mailbox) -> Event:
    return service.status_event(mailbox.id, mailbox.user_id, mailbox.status, mailbox.status_reason)


@router.get("/mailboxes", response_model=list[MailboxOut])
def list_mailboxes(session: SessionDep, user: CurrentUser) -> list[Mailbox]:
    """Every mailbox, disconnected ones included, oldest first."""
    return service.list_mailboxes(session, user)


@router.get("/mailboxes/status", responses=NO_KEYCHAIN)
def mailbox_status(request: Request, session: SessionDep, user: CurrentUser) -> MailboxStatusOut:
    """Whether a client is set, every mailbox, whether any needs re-authorizing, and how
    far each connected one is on the way to arming to send."""
    return _status(request, session, user)


@router.put("/mailboxes/oauth/client", responses={**INVALID, **NO_KEYCHAIN})
@read_only  # writes the Keychain, never the database
def set_oauth_client(
    body: OAuthClientIn, request: Request, session: SessionDep, user: CurrentUser
) -> MailboxStatusOut:
    """Store the OAuth client's ID and secret in the Keychain; answers the status after."""
    with translate_errors():
        service.save_client(user, gmail_oauth.validate_client(body.client_id, body.client_secret))
    return _status(request, session, user)


def _arm_steps(
    session: Session, user: User, mailbox: Mailbox, sender: GmailSender | None
) -> MailboxArmStepsOut:
    steps = message_id_check.arm_steps(session, user, mailbox, now=utcnow())
    return MailboxArmStepsOut(
        mailbox_id=steps.mailbox_id,
        armed_for_drafts=steps.armed_for_drafts,
        armed_to_send=steps.armed_to_send,
        verified_at=steps.verified_at,
        waiting=steps.waiting,
        checked_at=steps.checked_at,
        check_requested_at=None if sender is None else sender.drafts_poll_requested_at(user.id),
        checks_running=sender is not None,
        can_arm_send=steps.can_arm_send,
        next_step=steps.next_step,
        test_campaign_id=steps.test_campaign_id,
    )


def _status(request: Request, session: Session, user: User) -> MailboxStatusOut:
    with translate_errors():
        client = service.load_client(user.id)
    rows = service.list_mailboxes(session, user)
    sender = _gmail_sender(request)
    return MailboxStatusOut(
        client_configured=client is not None,
        client_id=None if client is None else client.client_id,
        keychain_service=keychain.service_name(),
        mailboxes=[MailboxOut.model_validate(row) for row in rows],
        reauth_required=any(row.status is MailboxStatus.REAUTH_REQUIRED for row in rows),
        arm_steps=[
            _arm_steps(session, user, row, sender)
            for row in rows
            if row.status is not MailboxStatus.DISABLED
        ],
    )


@router.post("/mailboxes/oauth/start", responses={**NOT_FOUND, **NO_CLIENT, **NO_KEYCHAIN})
@read_only  # the authorization waits in memory, never in the database
def start_oauth(
    body: OAuthStartIn, request: Request, session: SessionDep, user: CurrentUser
) -> OAuthStartOut:
    """The Google URL to open. Valid for ten minutes, once."""
    with translate_errors():
        hint = None
        if body.mailbox_id is not None:
            hint = service.get_mailbox(session, user, body.mailbox_id).email
        client = service.require_client(user.id)
        authorization = gmail_oauth.begin(
            client,
            str(request.url_for(CALLBACK_ROUTE)),
            login_hint=hint,
            endpoints=_endpoints(request),
        )
    _pending(request).add(user.id, authorization, body.return_to)
    return OAuthStartOut(authorization_url=authorization.url)


@router.get(
    "/mailboxes/oauth/callback",
    name=CALLBACK_ROUTE,
    status_code=303,
    response_class=RedirectResponse,
    responses={303: {"description": "Back to the page the flow started from, with the outcome"}},
)
def oauth_callback(
    request: Request,
    state: Annotated[str, Query(max_length=200)] = "",
    code: Annotated[str, Query(max_length=2000)] = "",
    error: Annotated[str, Query(max_length=200)] = "",
) -> RedirectResponse:
    """Where Google sends the browser back. Always redirects to an app page: the one the
    flow started from (#656), or Settings."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    auth: AuthProvider = request.app.state.auth
    with session_scope(factory) as session:
        user_id = auth.current_user(request, session).id
    taken = _pending(request).take_with_return(state, user_id) if state else None
    if taken is None:
        return _back("error", "state_mismatch")
    authorization, return_to = taken
    if error:
        # Google's own code, e.g. access_denied when the person pressed Cancel.
        return _back("error", error if error.replace("_", "").isalnum() else "denied", return_to)
    if not code:
        return _back("error", "no_code", return_to)
    try:
        client = service.require_client(user_id)
        grant = gmail_oauth.exchange_code(
            client, authorization, code, endpoints=_endpoints(request)
        )
        email = gmail_oauth.fetch_email(grant.access_token, endpoints=_endpoints(request))
    except service.ClientNotConfigured:
        return _back("error", "client_missing", return_to)
    except gmail_oauth.OAuthError as exc:
        log.info("gmail authorization failed: %s", exc.code)
        return _back("error", exc.code, return_to)
    except keychain.KeychainUnavailable as exc:
        log.warning("gmail authorization failed: %s", personal_exc(exc))
        return _back("error", "keychain", return_to)
    try:
        with session_scope(factory, write=True) as session:
            user = auth.current_user(request, session)
            settings = effective_settings(request, session, user)
            mailbox = service.connect(
                session,
                user,
                email,
                grant.refresh_token,
                daily_cap=settings.campaigns.mailbox_daily_cap,
            )
            event = _status_event(mailbox)
    except service.OtherMailboxConnected:
        return _back("error", "other_mailbox_connected", return_to)
    except keychain.KeychainUnavailable as exc:
        log.warning("gmail authorization failed: %s", personal_exc(exc))
        return _back("error", "keychain", return_to)
    _publish(request, event)
    return _back("connected", None, return_to)


@router.post("/mailboxes/{mailbox_id}/check", responses={**NOT_FOUND, **NO_KEYCHAIN})
@read_only  # the check opens its own writer after Google answers, never during
def check_mailbox(mailbox_id: int, request: Request, user: CurrentUser) -> MailboxOut:
    """Refresh the token now, as the background poll does; answers the mailbox after.

    While ``serve`` runs and the mailbox is armed and ``ok``, it also asks for the
    drafts check, with its Message-ID check, at the next tick (#510). That never calls
    Gmail here: the tick runs the check, gated as usual."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    result = service.check_mailbox(factory, user.id, mailbox_id, endpoints=_endpoints(request))
    if result is None:
        raise HTTPException(status_code=404, detail=f"no mailbox {mailbox_id}")
    sender = _gmail_sender(request)
    if sender is not None and result.status is MailboxStatus.OK:
        with session_scope(factory) as session:
            armed = service.armed(session, user, mailbox_id) is not None
        if armed:
            sender.request_drafts_poll(user.id)
    if result.changed:
        _publish(
            request,
            service.status_event(result.mailbox_id, result.user_id, result.status, result.reason),
        )
    with session_scope(factory) as session, translate_errors():
        return MailboxOut.model_validate(service.get_mailbox(session, user, mailbox_id))


@router.post("/mailboxes/{mailbox_id}/disconnect", responses={**NOT_FOUND, **NO_KEYCHAIN})
def disconnect_mailbox(
    mailbox_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> MailboxOut:
    """Forget the token and disable the mailbox. Its row stays for the campaigns naming it."""
    with translate_errors():
        mailbox = service.disconnect(session, user, service.get_mailbox(session, user, mailbox_id))
    out = MailboxOut.model_validate(mailbox)
    event = _status_event(mailbox)
    session.commit()  # before the event, so a listener that reads again sees it
    _publish(request, event)
    return out


@router.post("/mailboxes/{mailbox_id}/arm", responses={**NOT_FOUND, **NOT_ARMABLE})
def arm_mailbox(
    mailbox_id: int, body: MailboxArmIn, session: SessionDep, user: CurrentUser
) -> MailboxOut:
    """Let ``serve`` use the mailbox: ``draft`` first, ``send`` as a separate step (#277).
    Applies from the engine's next claim."""
    with translate_errors():
        mailbox = service.arm(
            session,
            user,
            service.get_mailbox(session, user, mailbox_id),
            body.mode,
            by=f"web (user {user.id})",
            now=utcnow(),
        )
    return MailboxOut.model_validate(mailbox)


@router.post("/mailboxes/{mailbox_id}/disarm", responses=NOT_FOUND)
def disarm_mailbox(mailbox_id: int, session: SessionDep, user: CurrentUser) -> MailboxOut:
    """Stop ``serve`` using the mailbox from the engine's next claim (#277)."""
    with translate_errors():
        mailbox = service.disarm(session, user, service.get_mailbox(session, user, mailbox_id))
    return MailboxOut.model_validate(mailbox)


def _back(outcome: str, reason: str | None, return_to: str = SETTINGS_PATH) -> RedirectResponse:
    """Redirect to ``return_to`` (checked when the flow started) with the outcome in its query.

    An existing query stays, except any ``gmail`` or ``reason`` it carried."""
    path, _, existing = return_to.partition("?")
    kept = [(k, v) for k, v in parse_qsl(existing, keep_blank_values=True) if k not in OUTCOME_KEYS]
    kept.append(("gmail", outcome))
    if reason is not None:
        kept.append(("reason", reason))
    return RedirectResponse(f"{path}?{urlencode(kept)}", status_code=303)
