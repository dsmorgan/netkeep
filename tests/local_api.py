"""The local API key's credential for a test's HTTP client (#618).

Every ``/api/`` request needs the session cookie or ``Authorization: Bearer <key>``.
A test client built on an app passes ``auth=LocalKey(app)``: the key is read when each
request is sent, because the app makes it in its lifespan, after the client exists.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx
from fastapi import FastAPI


class LocalKey(httpx.Auth):
    """Send ``app``'s local API key as a Bearer token on every request.

    An app with no key (a bare ``FastAPI()`` a test builds without the middleware)
    gets no header."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        key = getattr(self.app.state, "local_api_key", None)
        if key is not None:
            request.headers["Authorization"] = f"Bearer {key.key}"
        yield request
