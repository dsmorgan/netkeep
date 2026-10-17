"""``/session``: connecting a browser to the local API (#618, ADR 0013; spec 14.2).

The browser's credential is the session cookie (:mod:`netkeeper.services.local_api_key`).
Three ways to get it, none of which needs the key itself in the browser:

* ``GET /session/connect?code=...``: the link ``netkeeper open`` and the launchers
  open. A good code sets the cookie and redirects to ``/``; a bad, spent or expired one
  redirects to ``/?connect=expired``, where the UI says so. The access log never shows
  the code (``netkeeper.logging_setup``).
* ``POST /session/claim``: the first browser after a key is made connects without a
  link. Once spent it answers ``403``. It needs the CSRF header like every state change.
* A browser that already has the cookie keeps it for months and across restarts.

``GET /session`` says which applies. These routes answer without the key
(``OPEN_API_PATHS`` in :mod:`netkeeper.web.security`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from netkeeper.services.keychain import KeychainUnavailable
from netkeeper.services.local_api_key import (
    COOKIE_MAX_AGE_S,
    LocalApiKey,
    cookie_name,
)
from netkeeper.web.schemas import SessionOut
from netkeeper.web.security import has_local_credential, request_port

router = APIRouter(tags=["session"])

CONNECTED_TO = "/"
EXPIRED_TO = "/?connect=expired"


@router.get("/session", operation_id="get_session")
def get_session(request: Request) -> SessionOut:
    key = _key(request)
    connected = has_local_credential(_headers(request), request.url.scheme, key)
    return SessionOut(connected=connected, claimable=not key.claimed)


@router.post(
    "/session/claim",
    operation_id="claim_session",
    status_code=204,
    responses={
        403: {"description": "A browser already claimed it; use `netkeeper open`"},
        503: {"description": "The Keychain refused to record the claim"},
    },
)
def claim_session(request: Request, response: Response) -> None:
    """Connect this browser without a link, if no browser has yet (ADR 0013 rule 6)."""
    key = _key(request)
    if has_local_credential(_headers(request), request.url.scheme, key):
        _set_cookie(response, request, key)  # already connected: refresh, spend nothing
        return
    try:
        claimed = key.claim()
    except KeychainUnavailable as exc:
        raise HTTPException(status_code=503, detail="the Keychain refused") from exc
    if not claimed:
        raise HTTPException(
            status_code=403,
            detail="another browser already connected; run `netkeeper open` to connect this one",
        )
    _set_cookie(response, request, key)


@router.get(
    "/session/connect",
    operation_id="connect_session",
    status_code=303,
    response_class=RedirectResponse,
    responses={303: {"description": "To the UI, connected, or to /?connect=expired"}},
)
def connect_session(
    request: Request, code: Annotated[str, Query(max_length=200)] = ""
) -> RedirectResponse:
    """Swap a connect link's one-time code for the session cookie."""
    key = _key(request)
    if not key.redeem(code):
        return RedirectResponse(EXPIRED_TO, status_code=303)
    response = RedirectResponse(CONNECTED_TO, status_code=303)
    _set_cookie(response, request, key)
    return response


def _key(request: Request) -> LocalApiKey:
    key: LocalApiKey = request.app.state.local_api_key
    return key


def _headers(request: Request) -> dict[str, str]:
    return {name.lower(): value for name, value in request.headers.items()}


def _set_cookie(response: Response, request: Request, key: LocalApiKey) -> None:
    response.set_cookie(
        cookie_name(request_port(_headers(request), request.url.scheme)),
        key.session_value(),
        max_age=COOKIE_MAX_AGE_S,
        path="/",
        httponly=True,
        samesite="strict",
    )
