"""``netkeeper serve``'s lock: one per data directory, held for as long as ``serve`` runs.

``serve`` holds ``<data dir>/locks/serve.lock`` from startup to shutdown, so a command
that must not run under a live server (``netkeeper restore``) can tell, and holds the
same lock while it works, so a ``serve`` started meanwhile refuses to start. It is the
same ``flock(2)`` mechanism as the browser activity lock
(:mod:`netkeeper.linkedin.activity_lock`): the kernel drops it when the process exits,
however it exits, so a crashed ``serve`` never leaves it held.
"""

from __future__ import annotations

from pathlib import Path

from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.activity_lock import Claim, Holder, LockState

SERVE_LOCK_FILENAME = "serve.lock"
_KEY = "serve"


class ServeRunning(RuntimeError):
    """Another process holds the serve lock: a running ``serve``, or a ``restore``."""

    def __init__(self, holder: Holder | None) -> None:
        self.holder = holder
        who = holder.describe() if holder is not None else "another netkeeper process"
        super().__init__(f"{who} holds {serve_lock_path()}")


def serve_lock_path() -> Path:
    """``<data dir>/locks/serve.lock``. Resolved per call, never cached."""
    return activity_lock.locks_dir() / SERVE_LOCK_FILENAME


def claim() -> Claim:
    """Take the serve lock without waiting; :class:`ServeRunning` when it is held."""
    path = serve_lock_path()
    held = activity_lock.try_claim_at(path, _KEY)
    if held is None:
        raise ServeRunning(state().holder)
    return held


def state() -> LockState:
    """Whether the serve lock is held right now, and by whom, without taking it."""
    return activity_lock.inspect_at(serve_lock_path(), _KEY)
