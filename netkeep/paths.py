"""Where netkeep keeps its data and where it looks for config (spec section 15).

Nothing here creates directories as a side effect. Callers that need the data
directory to exist call :func:`ensure_data_dir`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

log = logging.getLogger(__name__)

DATA_ENV = "NETKEEP_DATA"
CONFIG_ENV = "NETKEEP_CONFIG"
CONFIG_FILENAME = "config.toml"

ENV_PREFIX = "NETKEEP_"
LEGACY_ENV_PREFIX = "NETKEEPER_"
"""The environment variables' prefix before the rename (#661), read for one release."""

_warned: set[str] = set()


def legacy_env_name(name: str) -> str:
    """``NETKEEP_DATA`` -> ``NETKEEPER_DATA``: the name ``name`` had before the rename."""
    return LEGACY_ENV_PREFIX + name.removeprefix(ENV_PREFIX)


def getenv(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """``$name``, else its old ``NETKEEPER_`` name for one release (#661).

    The new name wins when both are set (to something other than blank). Reading the
    old one warns once per name and process, naming the new one.
    """
    env = os.environ if environ is None else environ
    value = env.get(name)
    if value is not None and value.strip():
        return value
    legacy = legacy_env_name(name)
    old = env.get(legacy)
    if old is None or not old.strip():
        return value
    if legacy not in _warned:
        _warned.add(legacy)
        log.warning(
            "%s is the old name of %s; rename it, the old name stops working in the next release",
            legacy,
            name,
        )
    return old


def data_dir() -> Path:
    """Return the data directory without creating it.

    Resolution: ``$NETKEEP_DATA``, else ``~/Library/Application Support/netkeep``
    on macOS, else ``./data`` relative to the current working directory.
    """
    override = getenv(DATA_ENV)
    if override:
        return Path(override).expanduser()
    return default_data_dir()


def default_data_dir() -> Path:
    """The data directory when ``$NETKEEP_DATA`` is unset, whether or not it is set."""
    if _is_macos():
        return Path.home() / "Library" / "Application Support" / "netkeep"
    return Path.cwd() / "data"


def legacy_default_data_dir() -> Path | None:
    """Where the default data directory was before the rename to netkeep (#661): macOS
    only, since elsewhere the default (``./data``) has no name in it."""
    if _is_macos():
        return Path.home() / "Library" / "Application Support" / "netkeeper"
    return None


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


def data_dir_digest(directory: Path) -> str:
    """A short hash of ``directory``'s resolved path: what tells two data directories
    apart in a name shared with the rest of the machine (the Keychain service, #580;
    the login item's label, #600)."""
    resolved = str(directory.expanduser().resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:12]


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

    ``explicit`` (from ``--config``), then ``$NETKEEP_CONFIG``, then
    ``./config.toml``, then ``<data_dir>/config.toml``. Entries need not exist.
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    override = getenv(CONFIG_ENV)
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path.cwd() / CONFIG_FILENAME)
    candidates.append(data_dir() / CONFIG_FILENAME)
    return candidates
