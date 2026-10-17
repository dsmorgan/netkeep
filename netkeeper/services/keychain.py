"""Secrets in the Keychain through ``keyring`` (spec 15; CLAUDE.md "Secrets").

Every secret is stored under the service :func:`service_name` with the name
``<user_id>/<name>``, so two users' secrets never share an entry. A secret that
belongs to the install rather than to a user (the local API key, #618) is named
``install/<name>``. Nothing here
logs a secret's value; a log line names the entry at most.

The service is per data directory (#580). Every fresh database has user 1 and
mailbox 1, so two data directories under one service would read, overwrite and
delete each other's Gmail client and token. The default data directory keeps
:data:`DEFAULT_SERVICE`, so an existing install keeps its secrets; any other one
uses ``netkeeper@<hash of its resolved path>``, unless
``$NETKEEPER_KEYCHAIN_SERVICE`` names the service. Every ``keyring`` call goes
through :func:`service_name`.

On macOS ``keyring`` uses the login Keychain. In the container it uses the file
backend under the data volume (spec 15). Tests install an in-memory backend
(``tests/conftest.py``), so no test touches a real Keychain.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from netkeeper import paths

log = logging.getLogger(__name__)

DEFAULT_SERVICE: Final = "netkeeper"
SERVICE_ENV: Final = "NETKEEPER_KEYCHAIN_SERVICE"


class KeychainUnavailable(RuntimeError):
    """The Keychain refused, or no usable backend is installed."""


def service_name() -> str:
    """The Keychain service this data directory's secrets are stored under.

    ``$NETKEEPER_KEYCHAIN_SERVICE`` when set; else :data:`DEFAULT_SERVICE` for the
    default data directory, however ``$NETKEEPER_DATA`` spells it; else
    ``netkeeper@`` and a short hash of the resolved data directory. Read at call
    time, like the data directory itself.
    """
    override = os.environ.get(SERVICE_ENV, "").strip()
    if override:
        return override
    directory = paths.data_dir()
    if paths.is_default_data_dir(directory):
        return DEFAULT_SERVICE
    return f"{DEFAULT_SERVICE}@{paths.data_dir_digest(directory)}"


def entry_name(user_id: int, name: str) -> str:
    """The entry's name under :func:`service_name`: ``<user_id>/<name>``."""
    if not name or name.startswith("/"):
        raise ValueError(f"a secret needs a relative name, not {name!r}")
    return f"{user_id}/{name}"


def install_entry_name(name: str) -> str:
    """An install-wide entry's name under :func:`service_name`: ``install/<name>``."""
    if not name or name.startswith("/"):
        raise ValueError(f"a secret needs a relative name, not {name!r}")
    return f"install/{name}"


def get_secret(user_id: int, name: str) -> str | None:
    """The secret stored as ``name`` for ``user_id``, or None when there is none."""
    return _get(entry_name(user_id, name))


def set_secret(user_id: int, name: str, value: str) -> None:
    """Store ``value`` as ``name`` for ``user_id``, replacing what was there."""
    _set(entry_name(user_id, name), value)


def delete_secret(user_id: int, name: str) -> bool:
    """Remove ``name`` for ``user_id``. Returns whether there was one to remove."""
    return _delete(entry_name(user_id, name))


def get_install_secret(name: str) -> str | None:
    """The install-wide secret stored as ``name``, or None when there is none."""
    return _get(install_entry_name(name))


def set_install_secret(name: str, value: str) -> None:
    """Store the install-wide ``value`` as ``name``, replacing what was there."""
    _set(install_entry_name(name), value)


def delete_install_secret(name: str) -> bool:
    """Remove the install-wide ``name``. Returns whether there was one to remove."""
    return _delete(install_entry_name(name))


def _get(entry: str) -> str | None:
    try:
        return keyring.get_password(service_name(), entry)
    except KeyringError as exc:
        raise KeychainUnavailable(_why(exc, "read", entry)) from exc


def _set(entry: str, value: str) -> None:
    if not value:
        raise ValueError("refusing to store an empty secret")
    try:
        keyring.set_password(service_name(), entry, value)
    except KeyringError as exc:
        raise KeychainUnavailable(_why(exc, "write", entry)) from exc
    log.info("stored secret %s (service %s)", entry, service_name())


def _delete(entry: str) -> bool:
    try:
        keyring.delete_password(service_name(), entry)
    except PasswordDeleteError:
        return False
    except KeyringError as exc:
        raise KeychainUnavailable(_why(exc, "delete", entry)) from exc
    log.info("deleted secret %s (service %s)", entry, service_name())
    return True


def _why(exc: KeyringError, verb: str, entry: str) -> str:
    # The backend's own message names the backend, never the value.
    return f"could not {verb} {entry} in the Keychain (service {service_name()}): {exc}"
