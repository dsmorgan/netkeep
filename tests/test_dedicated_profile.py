"""Attach only to netkeeper's dedicated Chrome profile (ADR 0012, rule 6; #624).

Offline, against fakes: no test here reaches a real Chrome or any port. The fake
Chrome (``browser_fakes.FakeConnector``) holds the profile it runs on the way Chrome
does, by writing the profile's ``SingletonLock`` for the pid it names, so these tests
move the fake to another profile, or change the pid, to describe each case.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from browser_fakes import FAKE_CHROME_PID, FakeBrowser, FakeConnector, FakeContext, hold_profile

from netkeeper import chrome_profile
from netkeeper.chrome_profile import (
    ProfileCheck,
    ProfileVerdict,
    browser_pid_from_process_info,
    check_dedicated_profile,
    profile_lock_pid,
)
from netkeeper.cli import _preflight_lines
from netkeeper.linkedin import browser
from netkeeper.linkedin.browser import (
    CHROME_PROFILE_DIRNAME,
    ActivityLocks,
    AttachBrowserProvider,
    BrowserUnavailable,
    ProfileRefused,
    read_browser_pid,
)
from netkeeper.linkedin.preflight import preflight
from netkeeper.paths import data_dir

CDP_URL = "http://127.0.0.1:9222"
OTHER_PID = 5151


def provider_for(connector: FakeConnector, profile: Path | None = None) -> AttachBrowserProvider:
    return AttachBrowserProvider(
        CDP_URL, connector=connector, locks=ActivityLocks(), profile=profile
    )


# --- the check itself ----------------------------------------------------------------


def test_the_browser_process_that_holds_the_profile_is_dedicated(tmp_path: Path) -> None:
    hold_profile(tmp_path, pid=FAKE_CHROME_PID)

    check = check_dedicated_profile(tmp_path, FAKE_CHROME_PID)

    assert check.verdict is ProfileVerdict.DEDICATED
    assert check.dedicated
    assert (check.browser_pid, check.lock_pid) == (FAKE_CHROME_PID, FAKE_CHROME_PID)


def test_a_browser_process_that_does_not_hold_the_profile_is_another_profile(
    tmp_path: Path,
) -> None:
    hold_profile(tmp_path, pid=OTHER_PID)

    check = check_dedicated_profile(tmp_path, FAKE_CHROME_PID)

    assert check.verdict is ProfileVerdict.OTHER_PROFILE
    assert not check.dedicated
    assert "isn't the one holding netkeeper's profile" in check.reason


def test_a_browser_that_names_no_process_is_undetermined(tmp_path: Path) -> None:
    hold_profile(tmp_path, pid=FAKE_CHROME_PID)

    check = check_dedicated_profile(tmp_path, None)

    assert check.verdict is ProfileVerdict.UNDETERMINED
    assert not check.dedicated


def test_a_profile_no_chrome_holds_is_undetermined(tmp_path: Path) -> None:
    """No lock: nothing shows the Chrome on the port is netkeeper's, so it's refused."""
    check = check_dedicated_profile(tmp_path / "never-started", FAKE_CHROME_PID)

    assert check.verdict is ProfileVerdict.UNDETERMINED
    assert "no Chrome holds netkeeper's profile" in check.reason


@pytest.mark.parametrize(
    "target", ["quorbelle-host", "quorbelle-host-", "quorbelle-host-12a", "quorbelle-host-0", ""]
)
def test_a_lock_without_a_pid_is_unreadable(tmp_path: Path, target: str) -> None:
    os.symlink(target, tmp_path / chrome_profile.SINGLETON_LOCK)

    assert profile_lock_pid(tmp_path) is None
    assert check_dedicated_profile(tmp_path, FAKE_CHROME_PID).verdict is ProfileVerdict.UNDETERMINED


def test_a_lock_that_is_a_plain_file_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / chrome_profile.SINGLETON_LOCK).write_text("quorbelle-host-4242")

    assert profile_lock_pid(tmp_path) is None


def test_the_lock_pid_ignores_a_hostname_with_dashes(tmp_path: Path) -> None:
    os.symlink("quorbelle-fictionary-mac.local-4242", tmp_path / chrome_profile.SINGLETON_LOCK)

    assert profile_lock_pid(tmp_path) == 4242


def test_the_lock_file_name_is_chromes() -> None:
    """Chrome's own file name, written out: a wrong one would refuse every attach."""
    assert chrome_profile.SINGLETON_LOCK == "SingletonLock"
    assert chrome_profile.BROWSER_PROCESS_TYPE == "browser"


def test_the_browser_pid_is_the_one_browser_entry() -> None:
    answer = {
        "processInfo": [
            {"type": "renderer", "id": 7001, "cpuTime": 0.1},
            {"type": "browser", "id": 4242, "cpuTime": 1.0},
            {"type": "GPU", "id": 7002, "cpuTime": 0.2},
        ]
    }

    assert browser_pid_from_process_info(answer) == 4242


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"processInfo": "browser"},
        {"processInfo": [{"type": "renderer", "id": 7001}]},
        {"processInfo": [{"type": "browser", "id": 4242}, {"type": "browser", "id": 4343}]},
        {"processInfo": [{"type": "browser", "id": "4242"}]},
        {"processInfo": [{"type": "browser", "id": True}]},
        {"processInfo": [{"type": "browser", "id": 0}]},
        {"processInfo": [{"type": "browser"}]},
    ],
)
def test_an_answer_without_one_clear_browser_pid_is_none(answer: Mapping[str, Any]) -> None:
    assert browser_pid_from_process_info(answer) is None


def test_the_refusal_names_the_profile_and_the_way_out(tmp_path: Path) -> None:
    message = check_dedicated_profile(tmp_path, None).refusal(CDP_URL)

    assert message.startswith(f"refusing to attach to the Chrome at {CDP_URL}: ")
    assert str(tmp_path) in message
    assert "`netkeeper browser launch`" in message


# --- reading the pid -------------------------------------------------------------------


class _Session:
    def __init__(self, answer: object = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.sent: list[tuple[str, object]] = []
        self.detached = False

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self.sent.append((method, params))
        if self.error is not None:
            raise self.error
        return self.answer

    async def detach(self) -> None:
        self.detached = True


class _SessionBrowser:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def new_browser_cdp_session(self) -> _Session:
        return self.session


async def test_the_pid_is_read_with_one_process_list_and_the_session_detaches() -> None:
    session = _Session({"processInfo": [{"type": "browser", "id": 4242}]})

    assert await read_browser_pid(_SessionBrowser(session)) == 4242
    assert session.sent == [("SystemInfo.getProcessInfo", None)]
    assert session.detached


@pytest.mark.parametrize(
    "session",
    [_Session(error=RuntimeError("'SystemInfo.getProcessInfo' wasn't found")), _Session("nope")],
)
async def test_a_failed_pid_read_is_none_and_the_session_still_detaches(session: _Session) -> None:
    assert await read_browser_pid(_SessionBrowser(session)) is None
    assert session.detached


async def test_a_browser_with_no_session_to_open_reads_as_none() -> None:
    assert await read_browser_pid(FakeBrowser()) is None


# --- the attach ------------------------------------------------------------------------


async def test_the_dedicated_profile_is_attached() -> None:
    connector = FakeConnector()
    provider = provider_for(connector)

    async with provider.run() as run:
        await run.ensure_page()
        assert run.profile_check.verdict is ProfileVerdict.DEDICATED
        assert run.profile_check.profile == data_dir() / CHROME_PROFILE_DIRNAME
    assert provider.profile == data_dir() / CHROME_PROFILE_DIRNAME


async def test_another_profile_is_refused_before_anything_is_touched(tmp_path: Path) -> None:
    """The Chrome on the port is another Chrome, while netkeeper's own runs elsewhere."""
    dedicated = data_dir() / CHROME_PROFILE_DIRNAME
    hold_profile(dedicated, pid=OTHER_PID)
    context = FakeContext()
    connector = FakeConnector(
        [FakeBrowser([context])], browser_pid=FAKE_CHROME_PID, profile=tmp_path / "everyday"
    )
    provider = provider_for(connector)

    with pytest.raises(ProfileRefused) as refused:
        async with provider.run():
            pytest.fail("a run was handed a Chrome that isn't netkeeper's profile")

    assert isinstance(refused.value, BrowserUnavailable), "callers treat it as unusable Chrome"
    assert refused.value.profile_check.verdict is ProfileVerdict.OTHER_PROFILE
    assert "refusing to attach" in str(refused.value)
    assert connector.detaches == 1
    assert context.pages == [], "no tab opened on the refused Chrome"


async def test_an_undeterminable_profile_is_refused() -> None:
    context = FakeContext()
    connector = FakeConnector([FakeBrowser([context])], browser_pid=None)
    provider = provider_for(connector)

    with pytest.raises(ProfileRefused) as refused:
        async with provider.run():
            pytest.fail("a run was handed a Chrome whose profile is unknown")

    assert refused.value.profile_check.verdict is ProfileVerdict.UNDETERMINED
    assert connector.detaches == 1
    assert context.pages == []


async def test_a_configured_profile_is_the_one_checked(tmp_path: Path) -> None:
    """The fake runs on the data directory's profile; a provider for another refuses."""
    provider = provider_for(FakeConnector(), profile=tmp_path / "configured")

    with pytest.raises(ProfileRefused):
        async with provider.run():
            pass


async def test_every_attach_makes_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if an attach skips the check: once per connection, the reattach's included,
    with the provider's profile and the pid that connection named."""
    calls: list[tuple[Path, int | None]] = []
    real = chrome_profile.check_dedicated_profile

    def spy(profile: Path, browser_pid: int | None) -> ProfileCheck:
        calls.append((profile, browser_pid))
        return real(profile, browser_pid)

    monkeypatch.setattr(browser, "check_dedicated_profile", spy)
    first, second = FakeBrowser(), FakeBrowser()
    connector = FakeConnector([first, second])
    provider = provider_for(connector)

    async with provider.run() as run:
        page = await run.ensure_page()
        await page.close()
        first.connected = False
        await run.ensure_page()

    profile = data_dir() / CHROME_PROFILE_DIRNAME
    assert connector.attaches == 2
    assert calls == [(profile, FAKE_CHROME_PID), (profile, FAKE_CHROME_PID)]


async def test_a_reattach_to_another_chrome_is_refused(tmp_path: Path) -> None:
    """The reattach is a new connection, so the first attach's answer isn't reused."""
    first, second = FakeBrowser(), FakeBrowser()
    connector = FakeConnector([first, second], browser_pid=[FAKE_CHROME_PID, None])
    provider = provider_for(connector)

    with pytest.raises(ProfileRefused):
        async with provider.run() as run:
            page = await run.ensure_page()
            await page.close()
            first.connected = False
            await run.ensure_page()


# --- preflight ------------------------------------------------


async def test_preflight_reports_the_refusal() -> None:
    connector = FakeConnector(browser_pid=None)

    report = await preflight(provider_for(connector))

    assert not report.attached
    assert not report.ok
    assert report.profile is not None
    assert report.profile.verdict is ProfileVerdict.UNDETERMINED
    assert any("refusing to attach" in problem for problem in report.problems)


async def test_preflight_reports_the_dedicated_profile() -> None:
    report = await preflight(provider_for(FakeConnector()))

    assert report.attached
    assert report.profile is not None
    assert report.profile.dedicated


def _profile_line(lines: list[str]) -> str:
    (line,) = [line for line in lines if line.split(maxsplit=1)[:1] == ["profile"]]
    return line


async def test_preflights_table_shows_the_profile_line() -> None:
    refused = await preflight(provider_for(FakeConnector(browser_pid=None)))
    passed = await preflight(provider_for(FakeConnector()))

    assert "refused, undetermined" in _profile_line(_preflight_lines(refused))
    line = _profile_line(_preflight_lines(passed))
    assert "netkeeper's (" in line
    assert f"pid {FAKE_CHROME_PID}" in line
