"""Error responses whose body carries more than ``{"detail": ...}``.

FastAPI's ``HTTPException`` always answers ``{"detail": <whatever was given>}``,
so a route that needs to say *why* with structured fields (the survivor of a
merged-away contact, the count a bulk action actually found) raises
:class:`ApiError` with the body it wants. :func:`install_error_handlers` wires
the handler on the app; the body is sent as given, so every module that raises
one documents its shape with a response model in ``responses``.

:class:`~netkeep.services.tasks.SubmitRefused` (the task runner takes no new task once a
restart is triggered, #730) answers ``409`` with its reason as the ``detail``, from
whichever route submitted.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from netkeep.services.tasks import SubmitRefused


class ApiError(Exception):
    """An HTTP error with a JSON body of the caller's choosing.

    ``body`` should carry a ``detail`` string like every other error, plus the
    fields the client needs to act on the error.
    """

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(body.get("detail", f"HTTP {status_code}"))


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return JSONResponse(status_code=exc.status_code, content=exc.body)


async def _handle_submit_refused(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, SubmitRefused)
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers for :class:`ApiError` and ``SubmitRefused`` on ``app``."""
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(SubmitRefused, _handle_submit_refused)
