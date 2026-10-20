"""Small file-system helpers shared across netkeep."""

from __future__ import annotations

import os
import tempfile
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


def write_atomic(path: Path, data: bytes | str, *, mode: int = 0o600) -> None:
    """Replace ``path`` with ``data`` so that a reader sees the old file or the new one,
    never part of either, and the new one is on disk when this returns.

    The data is written and fsynced under a unique temporary name in the same folder,
    renamed over ``path``, and the folder fsynced (:func:`sync_directory`). On any
    failure the temporary file is removed and ``path`` is left as it was. ``str`` data is
    written as UTF-8. ``netkeep update``'s state, scheduler marker and constraints files
    use it (#705).
    """
    payload = data.encode("utf-8") if isinstance(data, str) else data
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    sync_directory(path.parent)
