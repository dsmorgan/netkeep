"""The per-install local API key (#618, ADR 0013): the key, the cookie, the connect link.

Every test runs on the in-memory keyring ``tests/conftest.py`` installs; none touches a
real Keychain.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from gmail_fakes import MemoryKeyring
from local_api import LocalKey
from typer.testing import CliRunner

from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.logging_setup import (
    RedactQueryFilter,
    log_file,
    remove_file_handler,
    setup_logging,
)
from netkeeper.redaction import strip_secrets
from netkeeper.services import keychain, local_api_key
from netkeeper.services.local_api_key import LocalApiKey
from netkeeper.services.support_bundle import Level, build_bundle
from netkeeper.services.users import ensure_local_user
from netkeeper.web.security import OPEN_API_PATHS, RULE_LOCAL_KEY

CSRF = {"X-Netkeeper-Client": "1"}
BASE = "http://127.0.0.1"
COOKIE = "netkeeper_session_80"  # the client's base URL has no port: http's 80
NOW = 1_800_000_000.0


@pytest.fixture
async def anonymous(running_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no credential: what any other program on the machine starts as."""
    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield client


def _key(app: FastAPI) -> LocalApiKey:
    key: LocalApiKey = app.state.local_api_key
    return key


def _code(link: str) -> str:
    return parse_qs(urlsplit(link).query)["code"][0]


# --- the pinned values ---------------------------------------------------------------


def test_the_open_paths_are_pinned() -> None:
    assert (
        frozenset(
            {
                "/api/v1/health",
                "/api/v1/session",
                "/api/v1/session/connect",
                "/api/v1/session/claim",
                "/api/v1/mailboxes/oauth/callback",
            }
        )
        == OPEN_API_PATHS
    )


def test_the_code_and_cookie_lifetimes_are_pinned() -> None:
    assert local_api_key.CODE_TTL_S == 120
    assert local_api_key.COOKIE_MAX_AGE_S == 34_560_000  # 400 days


# --- the key and its credentials --------------------------------------------------------


def test_a_connect_code_works_once_until_it_expires() -> None:
    key = LocalApiKey("k" * 43)
    code = local_api_key.mint_code(key.key, now=NOW)
    assert key.redeem(code, now=NOW + 119)
    assert not key.redeem(code, now=NOW + 1)  # spent

    late = local_api_key.mint_code(key.key, now=NOW)
    assert not key.redeem(late, now=NOW + 120)  # expired


@pytest.mark.parametrize(
    "mangle",
    [
        lambda code: code[:-1] + ("0" if code[-1] != "0" else "1"),  # the signature
        lambda code: str(int(code.split(".")[0]) + 1) + code[code.index(".") :],  # the expiry
        lambda code: "",
        lambda code: "not.a.code",
        lambda code: code.replace(".", "", 1),
        lambda code: "١٢٣" + code[code.index(".") :],  # non-ASCII digits
    ],
)
def test_a_tampered_or_malformed_code_is_refused(mangle: object) -> None:
    key = LocalApiKey("k" * 43)
    code = local_api_key.mint_code(key.key, now=NOW)
    assert callable(mangle)
    assert not key.redeem(mangle(code), now=NOW)


def test_a_code_from_another_key_or_lasting_too_long_is_refused() -> None:
    key = LocalApiKey("k" * 43)
    assert not key.redeem(local_api_key.mint_code("other" * 9, now=NOW), now=NOW)
    # Signed with the right key, but claiming a lifetime no code is made with.
    far = int(NOW) + local_api_key.CODE_TTL_S + 60
    nonce = "ab" * 16
    forged = f"{far}.{nonce}.{local_api_key._sign(key.key, far, nonce)}"
    assert not key.redeem(forged, now=NOW)


def test_the_cookie_holds_a_value_derived_from_the_key_never_the_key() -> None:
    key = LocalApiKey("the-key-itself-0123456789")
    value = key.session_value()
    assert key.key not in value
    assert key.accepts_cookie(value)
    assert not key.accepts_cookie(None)
    assert not key.accepts_cookie("")
    assert not key.accepts_cookie(LocalApiKey("another").session_value())
    assert key.key not in repr(key)


def test_the_bearer_key_is_compared_whole() -> None:
    key = LocalApiKey("the-key")
    assert key.accepts_bearer("Bearer the-key")
    assert key.accepts_bearer("bearer the-key")
    assert not key.accepts_bearer("Bearer the-ke")
    assert not key.accepts_bearer("Basic the-key")
    assert not key.accepts_bearer("Bearer ")
    assert not key.accepts_bearer(None)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:8000"),
        ("0.0.0.0", "http://127.0.0.1:8000"),
        ("::", "http://127.0.0.1:8000"),
        ("::1", "http://[::1]:8000"),
        ("[::1]", "http://[::1]:8000"),
        ("localhost", "http://localhost:8000"),
    ],
)
def test_the_base_url_is_a_loopback_origin(host: str, expected: str) -> None:
    assert local_api_key.base_url(host, 8000) == expected


# --- first run ------------------------------------------------------------------------


def test_the_first_start_makes_the_key_in_the_keychain(memory_keyring: MemoryKeyring) -> None:
    assert keychain.get_install_secret(local_api_key.KEY_ENTRY) is None
    first = local_api_key.load_or_create()
    assert first.persisted
    assert not first.claimed
    stored = memory_keyring.entries[(keychain.service_name(), "install/local-api-key")]
    assert stored == first.key
    assert len(first.key) >= 40
    again = local_api_key.load_or_create()
    assert again.key == first.key


def test_the_claim_is_kept_in_the_keychain_and_a_new_key_opens_it_again() -> None:
    key = local_api_key.load_or_create()
    assert key.claim()
    assert not key.claim()
    assert local_api_key.load_or_create().claimed
    keychain.delete_install_secret(local_api_key.KEY_ENTRY)
    fresh = local_api_key.load_or_create()
    assert not fresh.claimed


def test_a_refusing_keychain_falls_back_to_a_key_in_memory(
    memory_keyring: MemoryKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    memory_keyring.broken = True
    with caplog.at_level(logging.INFO):
        key = local_api_key.load_or_create()
    assert not key.persisted
    assert not key.claimed
    assert key.claim()  # in memory only: nothing to record
    assert "lives in memory" in caplog.text
    assert key.key not in caplog.text


# --- the API -------------------------------------------------------------------------


async def test_a_request_without_the_credential_is_refused(
    anonymous: httpx.AsyncClient,
) -> None:
    for method, path in [
        ("GET", "/api/v1/me"),
        ("GET", "/api/v1/contacts"),
        ("GET", "/api/v1/events"),
        ("POST", "/api/v1/tasks/ping"),
        ("GET", "/api/v1/no-such-path"),  # no route is revealed either
    ]:
        response = await anonymous.request(method, path, headers=CSRF)
        assert response.status_code == 401, path
        assert response.json()["rule"] == RULE_LOCAL_KEY
        assert response.headers["www-authenticate"] == "Bearer"


async def test_a_wrong_credential_is_refused(
    running_app: FastAPI, anonymous: httpx.AsyncClient
) -> None:
    key = _key(running_app)
    wrong_key = {"Authorization": "Bearer not-the-key"}
    assert (await anonymous.get("/api/v1/me", headers=wrong_key)).status_code == 401
    forged = {"Cookie": f"{COOKIE}={LocalApiKey('guess').session_value()}"}
    assert (await anonymous.get("/api/v1/me", headers=forged)).status_code == 401
    # The right value under another port's name belongs to another server.
    other_port = {"Cookie": f"netkeeper_session_8001={key.session_value()}"}
    assert (await anonymous.get("/api/v1/me", headers=other_port)).status_code == 401
    # The key itself is no cookie.
    as_cookie = {"Cookie": f"{COOKIE}={key.key}"}
    assert (await anonymous.get("/api/v1/me", headers=as_cookie)).status_code == 401


async def test_the_bearer_key_or_the_cookie_lets_a_request_through(
    running_app: FastAPI, anonymous: httpx.AsyncClient
) -> None:
    key = _key(running_app)
    bearer = {"Authorization": f"Bearer {key.key}"}
    assert (await anonymous.get("/api/v1/me", headers=bearer)).status_code == 200
    # Another program's malformed cookie on the same host does not hide ours.
    cookie = {"Cookie": f'junk="a;b; {COOKIE}={key.session_value()}; other=1'}
    assert (await anonymous.get("/api/v1/me", headers=cookie)).status_code == 200
    posted = await anonymous.post("/api/v1/tasks/ping", headers={**bearer, **CSRF})
    assert posted.status_code == 202


async def test_the_open_paths_answer_without_the_credential(anonymous: httpx.AsyncClient) -> None:
    assert (await anonymous.get("/api/v1/health")).status_code == 200
    assert (await anonymous.get("/api/v1/session")).json() == {
        "connected": False,
        "claimable": True,
    }
    callback = await anonymous.get("/api/v1/mailboxes/oauth/callback?state=x")
    assert callback.status_code == 303  # its own state check refuses it
    assert "state_mismatch" in callback.headers["location"]
    # The built UI's files hold no data and stay open.
    assert (await anonymous.get("/")).status_code == 200


async def test_the_other_checks_still_run_with_a_good_credential(
    running_app: FastAPI,
) -> None:
    key = _key(running_app)
    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(
        transport=transport, base_url=BASE, auth=LocalKey(running_app)
    ) as client:
        cross_site = {**CSRF, "Origin": "http://evil.example"}
        assert (await client.post("/api/v1/tasks/ping", headers=cross_site)).status_code == 403
        assert (await client.post("/api/v1/tasks/ping")).status_code == 403  # no CSRF header
    async with httpx.AsyncClient(
        transport=transport, base_url="http://evil.example:8000"
    ) as rebound:
        cookie = {"Cookie": f"netkeeper_session_8000={key.session_value()}"}
        assert (await rebound.get("/api/v1/me", headers=cookie)).status_code == 421


# --- connecting a browser --------------------------------------------------------------


async def test_the_first_browser_claims_the_session_once(
    running_app: FastAPI, anonymous: httpx.AsyncClient, memory_keyring: MemoryKeyring
) -> None:
    # A page on another site cannot claim it.
    cross = await anonymous.post(
        "/api/v1/session/claim", headers={**CSRF, "Origin": "http://evil.example"}
    )
    assert cross.status_code == 403
    assert (await anonymous.post("/api/v1/session/claim")).status_code == 403  # no header
    assert (await anonymous.get("/api/v1/session")).json()["claimable"] is True

    claimed = await anonymous.post("/api/v1/session/claim", headers=CSRF)
    assert claimed.status_code == 204
    set_cookie = claimed.headers["set-cookie"]
    assert set_cookie.startswith(f"{COOKIE}={_key(running_app).session_value()};")
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Max-Age=34560000" in set_cookie
    assert "Path=/" in set_cookie
    assert _key(running_app).key not in set_cookie
    # httpx keeps the cookie, as the browser would.
    assert (await anonymous.get("/api/v1/me")).status_code == 200
    assert (await anonymous.get("/api/v1/session")).json() == {
        "connected": True,
        "claimable": False,
    }
    assert (await anonymous.post("/api/v1/session/claim", headers=CSRF)).status_code == 204
    entry = (keychain.service_name(), "install/local-api-claimed")
    assert memory_keyring.entries[entry] == "1"

    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as second:
        refused = await second.post("/api/v1/session/claim", headers=CSRF)
        assert refused.status_code == 403
        assert "netkeeper open" in refused.json()["detail"]
        assert "set-cookie" not in refused.headers


async def test_a_connect_link_connects_a_browser_once(
    running_app: FastAPI, anonymous: httpx.AsyncClient
) -> None:
    link = local_api_key.connect_url(BASE)
    assert link.startswith(f"{BASE}/api/v1/session/connect?code=")
    path = link.removeprefix(BASE)

    connected = await anonymous.get(path)
    assert connected.status_code == 303
    assert connected.headers["location"] == "/"
    assert "HttpOnly" in connected.headers["set-cookie"]
    assert (await anonymous.get("/api/v1/me")).status_code == 200
    # Connecting by link spends nothing: the first-browser claim is still open.
    assert _key(running_app).claimed is False

    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as replay:
        again = await replay.get(path)
        assert again.status_code == 303
        assert again.headers["location"] == "/?connect=expired"
        assert "set-cookie" not in again.headers
        assert (await replay.get("/api/v1/me")).status_code == 401


async def test_the_cookie_survives_a_restart(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        value = _key(app).session_value()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            headers = {"Cookie": f"{COOKIE}={value}"}
            assert (await client.get("/api/v1/me", headers=headers)).status_code == 200


def test_netkeeper_open_prints_a_link_the_server_accepts() -> None:
    result = CliRunner().invoke(cli, ["open", "--port", "8123"])
    assert result.exit_code == 0, result.output
    [link] = re.findall(r"http://\S+", result.output)
    assert link.startswith("http://127.0.0.1:8123/api/v1/session/connect?code=")
    key = local_api_key.load_or_create()  # the key `open` made, as serve would load it
    assert key.redeem(_code(link))
    assert key.key not in result.output


def test_netkeeper_open_says_so_when_the_keychain_refuses(memory_keyring: MemoryKeyring) -> None:
    memory_keyring.broken = True
    result = CliRunner().invoke(cli, ["open"])
    assert result.exit_code == 1
    assert "Keychain" in result.output


# --- never in logs or the bundle --------------------------------------------------------


def test_the_connect_code_never_reaches_the_access_log() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", "/api/v1/session/connect?code=123.abc.def", "1.1", 303),
        None,
    )
    assert RedactQueryFilter().filter(record)
    assert "123.abc.def" not in record.getMessage()
    assert "/api/v1/session/connect?<redacted>" in record.getMessage()


def test_the_bundles_secret_pass_strips_the_credentials() -> None:
    text = (
        "Cookie: netkeeper_session_8000=deadbeef\n"
        "set netkeeper_session_8000=cafef00d\n"
        "Authorization: Bearer the-local-key\n"
    )
    stripped = strip_secrets(text)
    for value in ("deadbeef", "cafef00d", "the-local-key"):
        assert value not in stripped


async def test_the_key_never_reaches_the_log_or_the_support_bundle(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    setup_logging()
    try:
        with caplog.at_level(logging.DEBUG):
            async with app.router.lifespan_context(app):
                key = _key(app)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
                    link = local_api_key.connect_url(BASE)
                    await client.get(link.removeprefix(BASE))
                    await client.post("/api/v1/session/claim", headers=CSRF)
                    await client.get("/api/v1/me")
                    await client.get("/api/v1/me", headers={"Authorization": "Bearer wrong"})
                with session_scope(app.state.session_factory) as session:
                    user = ensure_local_user(session, settings=Settings())
                    bundles = [
                        build_bundle(session, user, settings=Settings(), level=level)
                        for level in Level
                    ]
    finally:
        remove_file_handler()
    secrets_ = (key.key, key.session_value(), _code(link))
    logged = "\n".join(
        line
        for line in log_file().read_text(encoding="utf-8").splitlines()
        if " httpx " not in line
    )
    assert "made the local API key" in logged  # the scan is not vacuous
    # The test's own HTTP client logs the URLs it requests (logger "httpx"); that is
    # the test talking, not the server, so it is left out of both scans.
    server = "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")
    for value in secrets_:
        assert value not in server
        assert value not in logged
    for bundle in bundles:
        with zipfile.ZipFile(io.BytesIO(bundle.data)) as archive:
            files = {name: archive.read(name).decode() for name in archive.namelist()}
        assert any("made the local API key" in text for text in files.values()), files.keys()
        for name, text in files.items():
            assert all(value not in text for value in secrets_), name
