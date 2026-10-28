"""``POST /linkedin/browser/check``: Check connection, a live look at Chrome and the session (#652).

It records a ``session_check`` run and submits it to the task runner, then answers
``202`` with the run and task ids, before any browser work (spec 9.9, CLAUDE.md). The
run attaches to netkeep's Chrome as every run does, reads the LinkedIn session from
the cookie jar the way ``netkeep preflight`` does, and detaches
(:mod:`netkeep.services.session_check`). Its result is on the run
(``GET /linkedin/runs/{id}``: ``counts.session_status``, ``counts.session_summary``,
``counts.session_warnings``) and, once it ends, in ``GET /linkedin/browser/health``.

It answers ``409`` with the reason while another run is running or another netkeep
process holds the browser, so one check, or one browser run, runs at a time. Owner
only, like ``POST /linkedin/browser/start``: netkeep's Chrome belongs to the install.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from netkeep.models.base import utcnow
from netkeep.services import runs, session_check
from netkeep.services.scheduled_runs import submit_run
from netkeep.web.deps import CurrentUser, SessionDep, Tasks, require_owner
from netkeep.web.schemas import RunAccepted

router = APIRouter(prefix="/linkedin", tags=["linkedin"])

_NO_WORKER = (
    "this netkeep process has no browser worker (it was not started by `netkeep serve`),"
    " so it cannot check the connection"
)


@router.post(
    "/browser/check",
    operation_id="check_linkedin_browser",
    status_code=202,
    responses={
        403: {"description": "Only the local owner account checks netkeep's Chrome"},
        409: {
            "description": "Another run is running, another process holds the browser, or"
            " netkeep is restarting"
        },
        503: {"description": "This process has no browser worker"},
    },
)
async def check_browser(
    request: Request, user: CurrentUser, session: SessionDep, tasks: Tasks
) -> RunAccepted:
    """Start a Check connection run; answers at once, before any browser work.

    The run visits no page, so it spends no budget, and the session flag, heat, and
    active hours don't stop it, as they don't stop ``netkeep preflight``. Once a restart
    is triggered it answers ``409`` and records no run (#730).
    """
    require_owner(session, user, "only the local owner account checks netkeep's Chrome")
    executor: runs.RunExecutor | None = request.app.state.executor
    if executor is None:
        raise HTTPException(status_code=503, detail=_NO_WORKER)
    tasks.check_open()  # #730: before anything is written
    try:
        run = session_check.start(session, user, now=utcnow())
    except runs.RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Committed before the task is submitted, so the worker's first read finds the run.
    run_id = run.id
    session.commit()
    task_id, _ = submit_run(tasks, executor, run_id, user.id)
    return RunAccepted(run_id=run_id, task_id=task_id)
