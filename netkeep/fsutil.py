"""Small file-system helpers shared across netkeep."""

from __future__ import annotations

import os
from pathlib import Path


def sync_directory(directory: Path) -> None:
    """Make a rename into ``directory`` durable by fsyncing the folder.

    Best effort: the file is already replaced, so a failure here (a file system that
    can't sync a directory) must not read as "not changed". ``config adopt``'s rewrite
    and ``netkeep update``'s state and scheduler marker (#685) use it.
    """
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)
