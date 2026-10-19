"""#675: the launcher's probe names netkeeper's Chrome even when it has no windows open.

On macOS, Chrome keeps running after you close its last window. Playwright's
``connect_over_cdp`` sets up a browser context as it attaches, and such a Chrome refuses
that ("Browser context management is not supported"), so the probe, which attached
through it, named no pid and ``netkeeper open`` refused its own Chrome as
``not_dedicated``. The probe now reads the pid with :class:`CdpIdentityReader`: one
``SystemInfo.getProcessInfo`` over the browser target's websocket, no context needed.

The reader's own tests drive a fake Chrome on a loopback port the OS picks (never a
personal Chrome's), the way the smoke suite drives a loopback server: no real Chrome and no site.
Everything else is fakes.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from browser_guard import RealBrowserBlocked, is_personal_cdp
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from netkeeper.chrome_spawn import ChromeLaunch, ChromeLauncher
from netkeeper.linkedin import activity_lock, browser
from netkeeper.linkedin.browser import (
    NO_WINDOWS_ERROR,
    ActivityLocks,
    BrowserUnavailable,
    CdpIdentityReader,
    PlaywrightCdpConnector,
    no_windows_message,
)
from netkeeper.services.chrome_launch import LaunchStatus, PortProbe, Refusal
from netkeeper.worker import launcher_probe

#: The real methods, read before ``tests/conftest.py``'s guard replaces them for a test.
#: Only this file's tests put them back, each against its own fake on a loopback port.
REAL_READ_PID = CdpIdentityReader.read_pid
REAL_CONNECT = PlaywrightCdpConnector.connect

CHROME_PID = 4242
OTHER_PID = 5353


def test_the_identity_read_constants_are_these_literals() -> None:
    assert browser.IDENTITY_READ_MAX_BYTES == 65536
    assert browser.PROBE_STEP_TIMEOUT_S == 5.0
    assert NO_WINDOWS_ERROR == "Browser context management is not supported"
    assert browser.VERSION_REQUEST == (
        "GET /json/version HTTP/1.1\r\nHost: {netloc}\r\nConnection: close\r\n\r\n"
    )


# --- a fake Chrome on loopback --------------------------------------------------------------


def _process_info(*pids: int) -> dict[str, Any]:
    return {
        "processInfo": [
            *({"type": "browser", "id": pid, "cpuTime": 1.0} for pid in pids),
            {"type": "GPU", "id": 9001, "cpuTime": 0.5},
        ]
    }


@dataclass
class _FakeChrome:
    """``/json/version`` and the browser target's websocket, as a Chrome with no windows
    answers them. ``ws_url`` builds the websocket ``/json/version`` names from the
    request's own Host; ``before`` are messages sent ahead of the answer."""

    result: dict[str, Any] = field(default_factory=lambda: _process_info(CHROME_PID))
    ws_url: Callable[[str], str] = lambda host: f"ws://{host}/devtools/browser/0a1b-2c3d"
    version_status: HTTPStatus = HTTPStatus.OK
    version_body: str | None = None
    before: list[dict[str, Any]] = field(default_factory=list)
    received: list[dict[str, Any]] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    #: Answer the websocket handshake with ``302 Location: <redirect>`` instead.
    redirect: str | None = None
    sockets: int = 0
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        self.hosts.append(request.headers["Host"])
        if request.path != "/json/version":
            if self.redirect is not None:
                moved = connection.respond(HTTPStatus.FOUND, "")
                moved.headers["Location"] = self.redirect
                return moved
            return None  # the websocket handshake
        self.gets.append(request.path)
        body = self.version_body
        if body is None:
            body = json.dumps({"webSocketDebuggerUrl": self.ws_url(request.headers["Host"])})
        response = connection.respond(self.version_status, body)
        response.headers["Content-Type"] = "application/json"
        return response

    async def handler(self, ws: ServerConnection) -> None:
        self.sockets += 1
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                for event in self.before:
                    await ws.send(json.dumps(event))
                await ws.send(json.dumps({"id": message["id"], "result": self.result}))
        finally:
            self.closed.set()


#: The ports this module's fake Chromes listen on, while each runs. The reader's GET and
#: websocket may reach these and nothing else (#675 review: a mutant reached 9222).
_FAKE_PORTS: set[int] = set()


@asynccontextmanager
async def _running(chrome: _FakeChrome) -> AsyncIterator[str]:
    async with serve(
        chrome.handler, "127.0.0.1", 0, process_request=chrome.process_request
    ) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        assert not is_personal_cdp(url)
        _FAKE_PORTS.add(port)
        try:
            yield url
        finally:
            _FAKE_PORTS.discard(port)


@pytest.fixture
def real_reader(monkeypatch: pytest.MonkeyPatch) -> CdpIdentityReader:
    """The real reader, whose sockets can connect only to a running fake Chrome.

    Every connect the reader makes, the GET's and the websocket's, a redirect's
    included, goes through ``socket.socket.connect`` (asyncio calls it), so a mutant
    that points either anywhere else fails loudly here, before any byte leaves."""
    real_connect = socket.socket.connect

    def only_a_fake(self: socket.socket, address: Any) -> None:
        if not (isinstance(address, tuple) and address[:2] in _fake_addresses()):
            raise RealBrowserBlocked(f"the reader tried to reach {address!r}, not a fake")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", only_a_fake)
    monkeypatch.setattr(CdpIdentityReader, "read_pid", REAL_READ_PID)
    return CdpIdentityReader()


def _fake_addresses() -> set[tuple[str, int]]:
    # ::1 on a fake's port too: nothing listens there, which the localhost test needs.
    return {(host, port) for port in _FAKE_PORTS for host in ("127.0.0.1", "::1")}


async def test_the_reader_sends_one_process_info_and_closes(
    real_reader: CdpIdentityReader,
) -> None:
    chrome = _FakeChrome()
    async with _running(chrome) as url:
        assert await real_reader.read_pid(url) == CHROME_PID
        await asyncio.wait_for(chrome.closed.wait(), 2)
    assert chrome.gets == ["/json/version"]
    assert chrome.received == [{"id": 1, "method": "SystemInfo.getProcessInfo"}]
    assert chrome.sockets == 1


async def test_the_reader_skips_events_before_the_answer(real_reader: CdpIdentityReader) -> None:
    chrome = _FakeChrome(before=[{"method": "Target.targetCreated", "params": {}}, {"id": 7}])
    async with _running(chrome) as url:
        assert await real_reader.read_pid(url) == CHROME_PID


@pytest.mark.parametrize(
    "ws_url",
    [
        # Each is no pid, and the reader opens no websocket: only the browser target on
        # the address you configured counts.
        lambda host: f"ws://127.0.0.2:{host.rsplit(':', 1)[1]}/devtools/browser/0a1b",
        lambda host: f"ws://{host.rsplit(':', 1)[0]}:1/devtools/browser/0a1b",
        lambda host: f"wss://{host}/devtools/browser/0a1b",
        lambda host: f"ws://{host}/devtools/page/0a1b",
        lambda host: f"ws://{host}/devtools/browser/0a1b/../page/x",
        lambda host: f"ws://{host}/devtools/browser/0a1b?x=1",
        lambda host: f"ws://{host}/devtools/browser/0a1b#x",
        lambda host: f"ws://user@{host}/devtools/browser/0a1b",
    ],
)
async def test_a_websocket_anywhere_else_is_no_pid(
    real_reader: CdpIdentityReader,
    monkeypatch: pytest.MonkeyPatch,
    ws_url: Callable[[str], str],
) -> None:
    # The websocket step is stubbed: even a reader whose check was removed connects to
    # nothing here, and the stub records that it was asked.
    opened: list[str] = []

    async def no_websocket(url: str) -> object:
        opened.append(url)
        return _process_info(CHROME_PID)

    monkeypatch.setattr(browser, "_read_process_info", no_websocket)
    chrome = _FakeChrome(ws_url=ws_url)
    async with _running(chrome) as url:
        assert await real_reader.read_pid(url) is None
    assert chrome.gets == ["/json/version"] and opened == []


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (HTTPStatus.NOT_FOUND, "{}"),
        (HTTPStatus.OK, "not json"),
        (HTTPStatus.OK, "[]"),
        (HTTPStatus.OK, '{"webSocketDebuggerUrl": 7}'),
        (HTTPStatus.OK, "{}"),
    ],
)
async def test_an_unreadable_version_answer_is_no_pid(
    real_reader: CdpIdentityReader, status: HTTPStatus, body: str
) -> None:
    chrome = _FakeChrome(version_status=status, version_body=body)
    async with _running(chrome) as url:
        assert await real_reader.read_pid(url) is None
    assert chrome.sockets == 0


@pytest.mark.parametrize(
    "result",
    [
        _process_info(CHROME_PID, OTHER_PID),  # two browsers: which one?
        _process_info(),  # none
        {"processInfo": [{"type": "browser", "id": "4242"}]},
        {},
    ],
)
async def test_an_answer_that_names_no_one_browser_is_no_pid(
    real_reader: CdpIdentityReader, result: dict[str, Any]
) -> None:
    chrome = _FakeChrome(result=result)
    async with _running(chrome) as url:
        assert await real_reader.read_pid(url) is None


@pytest.mark.parametrize(
    "cdp_url",
    [
        "ws://127.0.0.1:9333/devtools/browser/x",
        "https://127.0.0.1:9333",
        "9333",
        "http://127.0.0.1",  # no port: never a default
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:x",
    ],
)
async def test_the_reader_reads_only_an_http_address(
    real_reader: CdpIdentityReader, monkeypatch: pytest.MonkeyPatch, cdp_url: str
) -> None:
    asked: list[tuple[str, int]] = []

    async def no_get(host: str, port: int, netloc: str) -> str:
        asked.append((host, port))
        return f"ws://{netloc}/devtools/browser/0a1b"

    async def no_websocket(url: str) -> object:
        return _process_info(CHROME_PID)

    monkeypatch.setattr(browser, "_browser_websocket_url", no_get)
    monkeypatch.setattr(browser, "_read_process_info", no_websocket)
    assert await real_reader.read_pid(cdp_url) is None
    assert asked == []


async def test_a_port_that_isnt_listening_is_no_pid(real_reader: CdpIdentityReader) -> None:
    # A port the OS gave this test and took back: nothing listens on it now.
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
    _FAKE_PORTS.add(port)
    try:
        assert await real_reader.read_pid(f"http://127.0.0.1:{port}") is None
    finally:
        _FAKE_PORTS.discard(port)


async def test_a_redirected_handshake_is_refused_and_never_followed(
    real_reader: CdpIdentityReader,
) -> None:
    """#675 review: the websocket library follows redirects unless told not to. A 302 on
    the handshake, even to another fake that would answer, is no pid, and the second
    fake hears nothing."""
    elsewhere = _FakeChrome(result=_process_info(OTHER_PID))
    async with _running(elsewhere) as target:
        port = target.rsplit(":", 1)[1]
        front = _FakeChrome(redirect=f"ws://127.0.0.1:{port}/devtools/browser/zz")
        async with _running(front) as url:
            assert await real_reader.read_pid(url) is None
    assert front.gets == ["/json/version"] and front.sockets == 0
    assert elsewhere.hosts == [] and elsewhere.received == [] and elsewhere.sockets == 0


async def test_a_version_answer_that_isnt_200_is_no_pid_even_naming_a_websocket(
    real_reader: CdpIdentityReader,
) -> None:
    chrome = _FakeChrome(version_status=HTTPStatus.ACCEPTED)
    async with _running(chrome) as url:
        assert await real_reader.read_pid(url) is None
    assert chrome.gets == ["/json/version"] and chrome.sockets == 0


async def test_the_get_and_the_websocket_use_the_one_resolved_address(
    real_reader: CdpIdentityReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``localhost`` is resolved once. ::1 refuses (the fake listens on 127.0.0.1 only),
    so the GET falls to 127.0.0.1, and the websocket goes there too, and says so."""
    lookups: list[object] = []
    real_getaddrinfo = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def getaddrinfo(self: Any, host: Any, port: Any, **kwargs: Any) -> Any:
        if host == "localhost":
            lookups.append(host)
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]
        return await real_getaddrinfo(self, host, port, **kwargs)

    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", getaddrinfo)
    chrome = _FakeChrome()
    async with _running(chrome) as url:
        port = url.rsplit(":", 1)[1]
        assert await real_reader.read_pid(f"http://localhost:{port}") == CHROME_PID
    assert lookups == ["localhost"]
    assert chrome.hosts == [f"127.0.0.1:{port}", f"127.0.0.1:{port}"]


async def test_a_slow_version_answer_ends_at_the_deadline(
    real_reader: CdpIdentityReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A port that drips its answer a byte at a time can't hold the read past its one
    deadline: the GET runs on the event loop, with no thread left reading after."""
    monkeypatch.setattr(browser, "PROBE_STEP_TIMEOUT_S", 0.2)

    async def drip(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"HTTP/1.1 200 OK\r\n")
            while True:
                writer.write(b"X")
                await writer.drain()
                await asyncio.sleep(0.02)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(drip, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _FAKE_PORTS.add(port)
    try:
        async with server:
            pid = await asyncio.wait_for(real_reader.read_pid(f"http://127.0.0.1:{port}"), 2)
    finally:
        _FAKE_PORTS.discard(port)
    assert pid is None


@pytest.mark.parametrize(
    ("head", "length"),
    [
        ("HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n", 12),
        ("HTTP/1.1 200 OK\r\ncontent-length:3\r\nContent-Type: x\r\n\r\n", 3),
        ("HTTP/1.0 200\r\nContent-Length: 0\r\n\r\n", 0),
        ("HTTP/1.1 302 Found\r\nContent-Length: 12\r\n\r\n", None),
        ("HTTP/1.1 2000 OK\r\nContent-Length: 12\r\n\r\n", None),
        ("HTTP/1.1 200 OK\r\n\r\n", None),  # no length
        ("HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n", None),
        ("HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n", None),
        ("HTTP/1.1 200 OK\r\nContent-Length: 65537\r\n\r\n", None),
        ("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 3\r\n\r\n", None),
    ],
)
def test_only_a_200_with_one_bounded_length_is_read(head: str, length: int | None) -> None:
    assert browser._ok_content_length(head) == length


# --- the launcher, end to end with fakes ------------------------------------------------------


@dataclass
class _Reader:
    """The pid a Chrome with no windows names over the reader's websocket."""

    pid: int | None
    calls: list[str] = field(default_factory=list)

    async def read_pid(self, cdp_url: str) -> int | None:
        self.calls.append(cdp_url)
        return self.pid


@dataclass
class _Handed:
    """A hand-off: the launched Chrome passes its arguments on and exits 0."""

    launches: list[ChromeLaunch] = field(default_factory=list)
    pid: int = 6464

    def poll(self) -> int | None:
        return 0

    def __call__(self, launch: ChromeLaunch) -> _Handed:
        self.launches.append(launch)
        return self


def _launcher(tmp_path: Path, reader: _Reader, spawned: _Handed) -> ChromeLauncher:
    profile = tmp_path / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "SingletonLock").symlink_to(f"fakehost.local-{CHROME_PID}")
    locks = tmp_path / "locks"

    async def address() -> str:
        return "http://127.0.0.1:9333"

    async def probe(url: str) -> PortProbe:
        key = activity_lock.SINGLE_ACCOUNT_KEY
        return await launcher_probe(url, locks=ActivityLocks(locks), account=key, reader=reader)

    async def no_sleep(_s: float) -> None:
        return None

    return ChromeLauncher(
        cdp_url=address,
        profile=profile,
        app_url=lambda: "http://127.0.0.1:8123/api/v1/session/connect?code=1.ab.cd",
        ui_port=8123,
        probe=probe,
        run_holds_browser=lambda: False,
        pid_alive=lambda pid: pid == CHROME_PID,
        spawn=spawned,
        answers=lambda port: True,
        sleep=no_sleep,
        settle_s=0.3,
        platform="darwin",
    )


async def test_netkeepers_chrome_with_no_windows_gets_the_window(tmp_path: Path) -> None:
    reader, spawned = _Reader(pid=CHROME_PID), _Handed()
    result = await _launcher(tmp_path, reader, spawned).start()
    assert result.status is LaunchStatus.HANDED_OFF, result
    assert result.pid == CHROME_PID and reader.calls == ["http://127.0.0.1:9333"]
    [launch] = spawned.launches
    assert launch.port == 9333 and launch.app_url.startswith("http://127.0.0.1:8123/")


@pytest.mark.parametrize("pid", [OTHER_PID, None])
async def test_another_chrome_or_one_that_names_no_process_is_still_refused(
    tmp_path: Path, pid: int | None
) -> None:
    spawned = _Handed()
    result = await _launcher(tmp_path, _Reader(pid=pid), spawned).start()
    assert result.status is LaunchStatus.REFUSED and result.refusal is Refusal.NOT_DEDICATED
    assert spawned.launches == []


async def test_the_probe_never_attaches_with_playwright(tmp_path: Path) -> None:
    """The default probe is the reader, never the connector that fails without a window."""
    connector_calls: list[str] = []

    async def no_attach(self: object, cdp_url: str) -> object:
        connector_calls.append(cdp_url)
        raise AssertionError("the probe attached with Playwright")

    reads: list[str] = []

    async def read_pid(self: object, cdp_url: str) -> int | None:
        reads.append(cdp_url)
        return CHROME_PID

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PlaywrightCdpConnector, "connect", no_attach)
        patch.setattr(CdpIdentityReader, "read_pid", read_pid)
        probe = await launcher_probe(
            "http://127.0.0.1:9333",
            locks=ActivityLocks(tmp_path),
            account=activity_lock.SINGLE_ACCOUNT_KEY,
        )
    assert probe == PortProbe(busy=False, browser_pid=CHROME_PID)
    assert reads == ["http://127.0.0.1:9333"] and connector_calls == []


# --- a run's attach on a Chrome with no windows ------------------------------------------------


class _NoWindowsPlaywright:
    """``async_playwright()`` whose ``connect_over_cdp`` fails as Chrome with no windows."""

    def __init__(self) -> None:
        self.stopped = 0
        self.chromium = self

    def __call__(self) -> _NoWindowsPlaywright:
        return self

    async def start(self) -> _NoWindowsPlaywright:
        return self

    async def stop(self) -> None:
        self.stopped += 1

    async def connect_over_cdp(self, cdp_url: str) -> object:
        raise RuntimeError(
            "BrowserType.connect_over_cdp: Protocol error (Browser.setDownloadBehavior):"
            f" {NO_WINDOWS_ERROR}.\nCall log:\n  - <ws connected>"
        )


async def test_a_run_on_a_chrome_with_no_windows_says_to_reopen_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import playwright.async_api

    fake = _NoWindowsPlaywright()
    monkeypatch.setattr(PlaywrightCdpConnector, "connect", REAL_CONNECT)
    monkeypatch.setattr(playwright.async_api, "async_playwright", fake)
    with pytest.raises(BrowserUnavailable) as raised:
        await PlaywrightCdpConnector().connect("http://127.0.0.1:9333")
    assert str(raised.value) == no_windows_message("http://127.0.0.1:9333")
    assert "netkeeper open" in str(raised.value) and "no windows open" in str(raised.value)
    assert fake.stopped == 1


async def test_any_other_attach_failure_keeps_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import playwright.async_api

    class _Refused(_NoWindowsPlaywright):
        async def connect_over_cdp(self, cdp_url: str) -> object:
            raise RuntimeError("connect ECONNREFUSED 127.0.0.1:9333")

    monkeypatch.setattr(PlaywrightCdpConnector, "connect", REAL_CONNECT)
    monkeypatch.setattr(playwright.async_api, "async_playwright", _Refused())
    with pytest.raises(BrowserUnavailable, match="ECONNREFUSED") as raised:
        await PlaywrightCdpConnector().connect("http://127.0.0.1:9333")
    assert "no windows open" not in str(raised.value)


def test_the_reader_guard_is_in_place() -> None:
    """``tests/conftest.py`` replaced the reader for this test, as for every other."""
    assert CdpIdentityReader.read_pid is not REAL_READ_PID
