"""Where netkeeper keeps its data and where it looks for config (spec section 15).

Nothing here creates directories as a side effect. Callers that need the data
directory to exist call :func:`ensure_data_dir`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DATA_ENV = "NETKEEPER_DATA"
CONFIG_ENV = "NETKEEPER_CONFIG"
CONFIG_FILENAME = "config.toml"


def data_dir() -> Path:
    """Return the data directory without creating it.

    Resolution: ``$NETKEEPER_DATA``, else ``~/Library/Application Support/netkeeper``
    on macOS, else ``./data`` relative to the current working directory.
    """
    override = os.environ.get(DATA_ENV)
    if override:
        return Path(override).expanduser()
    return default_data_dir()


def default_data_dir() -> Path:
    """The data directory when ``$NETKEEPER_DATA`` is unset, whether or not it is set."""
    if _is_macos():
        return Path.home() / "Library" / "Application Support" / "netkeeper"
    return Path.cwd() / "data"


def is_default_data_dir(path: Path) -> bool:
    """Whether ``path`` is :func:`default_data_dir`, however it is spelled.

    Both are resolved (``~``, ``..``, symlinks), and when both exist they are
    compared as files, so a case-insensitive file system's other spelling counts too.
    """
    candidate = path.expanduser().resolve()
    default = default_data_dir().resolve()
    if candidate == default:
        return True
    try:
        return candidate.samefile(default)
    except OSError:  # one of them does not exist yet
        return False


def _is_macos() -> bool:
    # Read at call time (tests monkeypatch sys.platform) through a plain str so
    # mypy does not treat the other branch as unreachable on the host platform.
    platform: str = sys.platform
    return platform == "darwin"


def ensure_data_dir() -> Path:
    """Return the data directory, creating it (and parents) if needed."""
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_candidates(explicit: Path | None = None) -> list[Path]:
    """Return the config search list in resolution order.

    ``explicit`` (from ``--config``), then ``$NETKEEPER_CONFIG``, then
    ``./config.toml``, then ``<data_dir>/config.toml``. Entries need not exist.
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    override = os.environ.get(CONFIG_ENV)
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path.cwd() / CONFIG_FILENAME)
    candidates.append(data_dir() / CONFIG_FILENAME)
    return candidates
