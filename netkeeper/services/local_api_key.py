"""The per-install key for the local API (#618, ADR 0013; spec 14.2).

One random key per data directory lives in the Keychain (:mod:`netkeeper.services.keychain`,
entry ``install/local-api-key`` under the data directory's service). ``serve`` creates
it the first time it starts. Nothing writes it anywhere else: not the database, not a
file, not a log line, and no response carries it.

Three credentials come from it, and only these three:

* **The Bearer key.** A local client (a script, a future CLI command) sends
  ``Authorization: Bearer <key>``.
* **The session cookie.** The browser's credential: ``netkeeper_session_<port>``,
  ``HttpOnly`` and ``SameSite=Strict``, holding :meth:`LocalApiKey.session_value`, an
  HMAC of the key, never the key. It lasts :data:`COOKIE_MAX_AGE_S` and survives a
  restart, because it is derived from the key rather than from the process. The port
  is in the name because a cookie is scoped to a host, not a port: two netkeeper
  servers on one machine must not overwrite each other's.
* **The connect code.** ``<expires>.<nonce>.<hmac>``, put in the connect link
  (:func:`connect_url`) by whatever opens the UI. The server swaps it for the cookie
  (``GET /api/v1/session/connect``). It expires after :data:`CODE_TTL` and works once
  per process, because it passes through Chrome's command line and history.

**The first browser** (ADR 0013 rule 6). Until one claims it, the session is
claimable: ``POST /api/v1/session/claim`` sets the cookie with no code, so an upgrade
or a fresh install keeps working from a bookmark. The claim is recorded in the
Keychain (``install/local-api-claimed``) and is cleared only when a new key is made.

When the Keychain refuses, ``serve`` still starts, on a key that lives in memory only:
a browser has to connect again after each restart, and the first-browser claim opens
again with it. The log says so once.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlencode

from netkeeper.logging_setup import personal_exc
from netkeeper.services import keychain

log = logging.getLogger(__name__)

KEY_ENTRY: Final = "local-api-key"
CLAIMED_ENTRY: Final = "local-api-claimed"
CLAIMED_VALUE: Final = "1"

COOKIE_PREFIX: Final = "netkeeper_session"
#: 400 days, the longest Chrome keeps a cookie.
COOKIE_MAX_AGE_S: Final = 400 * 24 * 60 * 60
#: How long a connect code lasts, in seconds.
CODE_TTL_S: Final = 120
CONNECT_PATH: Final = "/api/v1/session/connect"
CODE_PARAM: Final = "code"

_WILDCARDS: Final = frozenset({"0.0.0.0", "::"})  # noqa: S104 (names, not a bind)
_KEY_BYTES: Final = 32
_NONCE_BYTES: Final = 16
_SESSION_LABEL: Final = b"netkeeper-ui-session"


@dataclass
class LocalApiKey:
    """The key ``serve`` checks requests against, and the state around it.

    ``persisted`` is False when the Keychain refused and the key lives in memory only.
    """

    key: str
    persisted: bool = True
    claimed: bool = False
    _used: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __repr__(self) -> str:  # never the key, even in a traceback
        return f"LocalApiKey(persisted={self.persisted}, claimed={self.claimed})"

    def session_value(self) -> str:
        """What the session cookie holds: an HMAC of the key, never the key itself."""
        return hmac.new(self.key.encode(), _SESSION_LABEL, hashlib.sha256).hexdigest()

    def accepts_cookie(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(str(value), self.session_value())

    def accepts_bearer(self, authorization: str | None) -> bool:
        """Whether an ``Authorization`` header carries this key as a Bearer token."""
        if not authorization:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return False
        return hmac.compare_digest(token.strip(), self.key)

    def redeem(self, code: str, *, now: float | None = None) -> bool:
        """Whether ``code`` is a connect code made from this key, unexpired and unused.

        A good code is spent: the same code a second time is refused.
        """
        moment = time.time() if now is None else now
        parsed = _parse(code)
        if parsed is None:
            return False
        expires, nonce, signature = parsed
        if not hmac.compare_digest(signature, _sign(self.key, expires, nonce)):
            return False
        # A code that claims to last longer than any code is made to is not one of ours.
        if not moment < expires <= moment + CODE_TTL_S:
            return False
        with self._lock:
            for spent, until in list(self._used.items()):
                if until <= moment:
                    del self._used[spent]
            if nonce in self._used:
                return False
            self._used[nonce] = expires
        return True

    def claim(self) -> bool:
        """Spend the first-browser claim. Returns False when it was already spent.

        Raises :class:`~netkeeper.services.keychain.KeychainUnavailable` when the
        Keychain refuses to record it; the claim is then left unspent.
        """
        with self._lock:
            if self.claimed:
                return False
            if self.persisted:
                keychain.set_install_secret(CLAIMED_ENTRY, CLAIMED_VALUE)
            self.claimed = True
        log.info("the first browser connected to the local API")
        return True


def load_or_create() -> LocalApiKey:
    """The data directory's key, made and stored the first time (``serve``'s startup).

    Falls back to a key in memory when the Keychain refuses (see the module docstring).
    """
    try:
        key = stored_key()
        claimed = keychain.get_install_secret(CLAIMED_ENTRY) == CLAIMED_VALUE
    except keychain.KeychainUnavailable as exc:
        log.warning(
            "the Keychain refused the local API key, so it lives in memory until netkeeper"
            " stops: a browser connects again after a restart (%s)",
            personal_exc(exc),
        )
        return LocalApiKey(_new_key(), persisted=False)
    return LocalApiKey(key, claimed=claimed)


def stored_key() -> str:
    """The stored key, made the first time, for a client outside ``serve``.

    Raises :class:`~netkeeper.services.keychain.KeychainUnavailable` when the Keychain
    refuses: a client has no use for a key ``serve`` will never see.
    """
    key = keychain.get_install_secret(KEY_ENTRY)
    if key is None:
        key = _new_key()
        keychain.delete_install_secret(CLAIMED_ENTRY)  # a new key opens the claim again
        keychain.set_install_secret(KEY_ENTRY, key)
        log.info("made the local API key (#618)")
    return key


def mint_code(key: str, *, now: float | None = None) -> str:
    """A connect code for ``key``, good once for :data:`CODE_TTL_S` seconds."""
    moment = time.time() if now is None else now
    expires = int(moment) + CODE_TTL_S
    nonce = secrets.token_hex(_NONCE_BYTES)
    return f"{expires}.{nonce}.{_sign(key, expires, nonce)}"


def connect_url(base_url: str, key: str | None = None, *, now: float | None = None) -> str:
    """The link that connects a browser: what ``netkeeper open`` and the launchers open.

    ``base_url`` is the server's origin (``http://127.0.0.1:8000``). ``key`` defaults to
    :func:`stored_key`.
    """
    code = mint_code(stored_key() if key is None else key, now=now)
    return f"{base_url.rstrip('/')}{CONNECT_PATH}?{urlencode({CODE_PARAM: code})}"


def base_url(host: str, port: int) -> str:
    """The origin a browser on this machine uses for a server bound to ``host``:``port``.

    A wildcard bind (``0.0.0.0``, ``::``) is reached on ``127.0.0.1``; the server
    answers only loopback names anyway (:mod:`netkeeper.web.security`).
    """
    name = host.strip("[]") or "127.0.0.1"
    if name in _WILDCARDS:
        name = "127.0.0.1"
    if ":" in name:
        name = f"[{name}]"
    return f"http://{name}:{port}"


def cookie_name(port: int) -> str:
    """The session cookie's name for a server the browser reaches on ``port``."""
    return f"{COOKIE_PREFIX}_{port}"


def _new_key() -> str:
    return secrets.token_urlsafe(_KEY_BYTES)


def _sign(key: str, expires: int, nonce: str) -> str:
    message = f"connect:{expires}:{nonce}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def _parse(code: str) -> tuple[int, str, str] | None:
    parts = code.split(".")
    if len(parts) != 3 or not (parts[0].isascii() and parts[0].isdigit()):
        return None
    expires, nonce, signature = parts
    if len(nonce) != _NONCE_BYTES * 2 or len(signature) != 64:
        return None
    return int(expires), nonce, signature
