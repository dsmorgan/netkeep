"""``netkeep/services/browser_launch.py``: the launch command as data (P2-12).

This is the module ``GET /linkedin/browser`` uses instead of importing
``netkeep.linkedin.browser`` (forbidden for anything under ``netkeep/web/`` --
see ``BROWSER_MODULES`` in ``tests/test_browser_safety.py``). It duplicates one
constant from that module rather than importing it; one test here pins the two
copies together so they cannot silently drift apart. ``netkeep browser launch``
(``netkeep/cli.py``) imports this module's functions directly rather than
keeping its own copy of the command-building logic (M3, #179 review); the last
test asserts its printed command is exactly what those functions return, so an
import forgetting to happen fails loudly rather than passing by coincidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from netkeep import cli as cli_module
from netkeep.cli import app as cli
from netkeep.config import LinkedInSettings, Settings
from netkeep.linkedin.browser import CHROME_PROFILE_DIRNAME as EXTRACTOR_PROFILE_DIRNAME
from netkeep.loopback import configured_port
from netkeep.paths import data_dir
from netkeep.services.browser_launch import (
    CHROME_PROFILE_DIRNAME,
    chrome_launch_command,
    no_port_note,
    remote_host_note,
)


def test_the_launch_port_is_the_configured_port() -> None:
    assert configured_port("http://127.0.0.1:9333") == 9333


@pytest.mark.parametrize("url", ["http://127.0.0.1", "not a url at all", "http://[::1"])
def test_a_url_with_no_port_gets_no_guessed_9222(url: str) -> None:
    """#701: no port is no port, never Chrome's usual 9222."""
    assert configured_port(url) is None


def test_macos_command_uses_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    lines = chrome_launch_command(9222, Path("/profile"))
    assert lines == [
        'open -na "Google Chrome" --args \\',
        "  --remote-debugging-port=9222 \\",
        '  --user-data-dir="/profile"',
    ]


def test_linux_command_uses_the_binary_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    lines = chrome_launch_command(9222, Path("/profile"))
    assert lines[0] == "google-chrome \\"
    assert lines[1] == "  --remote-debugging-port=9222 \\"
    assert lines[2] == '  --user-data-dir="/profile"'


def test_remote_host_note_is_none_for_loopback() -> None:
    for url in ("http://127.0.0.1:9222", "http://localhost:9222", "http://[::1]:9222"):
        assert remote_host_note(url) is None


@pytest.mark.parametrize("url", ["http://[::1", "http://[nope]:9222", "http://:9222", ""])
def test_remote_host_note_is_none_for_a_url_that_does_not_parse(url: str) -> None:
    """#701: ``urlsplit``'s ValueError was a 500 on GET /linkedin/browser and a CLI
    crash. No host is no remote host; config load and the port check refuse the url."""
    assert remote_host_note(url) is None


def test_remote_host_note_warns_off_loopback() -> None:
    note = remote_host_note("http://10.0.0.5:9222")
    assert note is not None
    assert "10.0.0.5" in note
    assert "loopback" in note


def test_the_duplicated_profile_dirname_matches_the_extractors() -> None:
    """The one thing this module copies instead of imports (see the module docstring).

    A mutation that lets the two spellings drift apart is caught here, not by
    `test_browser_safety.py`'s import scan, which only ever sees this module's
    constant, never the extractor's.
    """
    assert CHROME_PROFILE_DIRNAME == EXTRACTOR_PROFILE_DIRNAME


def test_cli_browser_launch_prints_exactly_this_modules_command() -> None:
    """`netkeep browser launch` calls `chrome_launch_command`/`configured_port` (M3);
    its printed lines must be exactly what those functions return, not a
    hand-kept copy that could say something different."""
    settings = Settings()
    profile = data_dir() / CHROME_PROFILE_DIRNAME
    port = configured_port(settings.linkedin.cdp_url)
    assert port is not None
    expected = chrome_launch_command(port, profile)

    result = CliRunner().invoke(cli, ["browser", "launch"])

    assert result.exit_code == 0, result.output
    for line in expected:
        assert f"  {line}" in result.output


def test_cli_browser_launch_json_is_what_scripts_chrome_sh_reads(tmp_path: Path) -> None:
    """`make chrome` starts Chrome from these fields, so they must be the configured ones."""
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://127.0.0.1:9333"\n')

    result = CliRunner().invoke(cli, ["--config", str(config), "browser", "launch", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "cdp_url": "http://127.0.0.1:9333",
        "port": 9333,
        "profile": str(data_dir() / CHROME_PROFILE_DIRNAME),
        "remote": None,
    }


def test_cli_browser_launch_json_names_a_remote_cdp_url(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://10.0.0.5:9222"\n')

    result = CliRunner().invoke(cli, ["--config", str(config), "browser", "launch", "--json"])

    assert result.exit_code == 0, result.output
    remote = json.loads(result.output)["remote"]
    assert isinstance(remote, str) and "10.0.0.5" in remote


def test_cli_browser_launch_uses_the_chrome_address_the_settings_page_set() -> None:
    """#597 review: `browser launch` (and so `make chrome`) uses the address in force,
    the file's, then the Settings page's, as serve and preflight do."""
    from sqlalchemy import select

    from netkeep.db import database_url, make_engine, make_session_factory, session_scope
    from netkeep.models import User
    from netkeep.scoping import install_scope_guard
    from netkeep.services import ui_settings

    assert CliRunner().invoke(cli, ["db", "upgrade"]).exit_code == 0
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = session.scalars(select(User)).one()
            ui_settings.write(
                session, user, Settings(), {"linkedin.cdp_url": "http://127.0.0.1:9444"}
            )
    finally:
        engine.dispose()

    result = CliRunner().invoke(cli, ["browser", "launch", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["port"] == 9444
    plain = CliRunner().invoke(cli, ["browser", "launch"])
    assert "--remote-debugging-port=9444" in plain.output


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_browser_launch_prints_no_command_without_a_port(
    monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    """#701: an address with no port is refused with the reason, and no 9222 command.
    config.toml and the Settings page both refuse one now, so the settings are stubbed."""
    settings = Settings(linkedin=LinkedInSettings(cdp_url="http://127.0.0.1"))
    monkeypatch.setattr(cli_module, "_load_settings_or_exit", lambda _state: settings)

    result = CliRunner().invoke(cli, ["browser", "launch", *(["--json"] if as_json else [])])

    assert result.exit_code == 1
    assert no_port_note("http://127.0.0.1") in result.output
    assert "9222" not in result.output.replace("http://127.0.0.1:9222", "")
    assert "--remote-debugging-port" not in result.output


def test_cli_browser_launch_refuses_a_config_cdp_url_that_does_not_parse(tmp_path: Path) -> None:
    """#701: a malformed config.toml cdp_url stops at load with the key named."""
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://[::1"\n')

    result = CliRunner().invoke(cli, ["--config", str(config), "browser", "launch", "--json"])

    assert result.exit_code != 0
    assert "linkedin.cdp_url" in result.output
    assert "--remote-debugging-port" not in result.output
