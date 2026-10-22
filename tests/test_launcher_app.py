"""The launcher app (#754): ``~/Applications/netkeep.app``, built, rebuilt and removed under
``tmp_path``, its script run against stubs, and ``netkeep open --launcher``.

Nothing here writes to the real ``~/Applications`` (conftest blocks the default folder),
runs the real ``launchctl``, ``osascript`` or ``open``, or starts Chrome. The script runs
with ``PATH`` limited to the test's own stub folder plus ``/usr/bin:/bin``, and ``HOME``
inside ``tmp_path``.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_login_item import FakeLaunchctl
from test_login_item import make as make_item
from typer.testing import CliRunner

from netkeep import __version__, cli, paths
from netkeep.services import keychain, launcher_app, local_api_key, login_item, post_install
from netkeep.services.chrome_launch import LaunchResult, LaunchStatus, Refusal
from netkeep.services.launcher_app import Launcher, LauncherError, ServeSeams, State
from netkeep.services.login_item import LoginItem, LoginItemError

UID = 501
BASE = "http://127.0.0.1:8123"


# --- helpers ---------------------------------------------------------------------------------


def _real_path_snapshot() -> dict[str, list[str]]:
    """Every folder on the test process's own ``PATH``, and what's in it."""
    found: dict[str, list[str]] = {}
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        try:
            found[folder] = sorted(os.listdir(folder))
        except OSError:
            found[folder] = []
    return found


@pytest.fixture(autouse=True)
def _nothing_on_the_real_path_changes() -> Iterator[None]:
    before = _real_path_snapshot()
    yield
    assert _real_path_snapshot() == before, "a launcher test changed a folder on the real PATH"


class Box:
    """A scratch home under ``tmp_path``: its Applications folder, a fake installed
    ``netkeep`` and stub ``launchctl``, ``osascript`` and ``open`` that log their calls."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.applications = self.home / "Applications"
        self.data = tmp_path / "nk-data"
        self.bin = tmp_path / "stub-bin"
        self.bin.mkdir(parents=True)
        self.home.mkdir()
        self.log = tmp_path / "calls.log"
        self.executable = tmp_path / "tool bin" / "net'keep"  # a space and a quote
        self.executable.parent.mkdir()
        for name in ("launchctl", "osascript", "open"):
            # Each argument in brackets, so where one ends is visible.
            self._stub(
                self.bin / name,
                f'{{ printf "%s" "{name} "; for a in "$@"; do printf "[%s]" "$a"; done; echo; }}'
                ' >> "$LOG"\nexit 0\n',
            )
        self.netkeep("exit 0\n")

    def _stub(self, path: Path, body: str) -> None:
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def netkeep(self, body: str) -> None:
        """What the fake installed ``netkeep`` does, after logging how it was run."""
        self._stub(
            self.executable,
            'printf "%s\\n" "netkeep $* data=$NETKEEP_DATA" >> "$LOG"\n' + body,
        )

    def launcher(self, **overrides: object) -> Launcher:
        arguments: dict[str, object] = {
            "data": self.data,
            "applications_dir": self.applications,
            "executable": self.executable,
            "macos": True,
            "environ": {},
            "osascript": str(self.bin / "osascript"),
            "version": "9.8.7",
        }
        arguments.update(overrides)
        return Launcher(**arguments)  # type: ignore[arg-type]

    def run_script(self, launcher: Launcher) -> tuple[int, list[str]]:
        """Run the bundle's executable the way LaunchServices does: no terminal, a small
        environment."""
        self.log.write_text("")
        script = launcher.path / "Contents" / "MacOS" / "netkeep"
        done = subprocess.run(
            [str(script)],
            env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "LOG": str(self.log)},
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return done.returncode, self.log.read_text().splitlines()


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# --- the pinned values -----------------------------------------------------------------------


def test_the_bundle_identity_is_pinned() -> None:
    assert launcher_app.BUNDLE_ID == "fun.tnkr.netkeep.launcher"
    assert launcher_app.MIN_MACOS == "14.0"
    assert launcher_app.OSASCRIPT == "/usr/bin/osascript"
    assert launcher_app.OPEN_ARGUMENTS == ("open", "--launcher")


def test_the_packaged_icon_is_an_icns() -> None:
    icon = launcher_app.packaged_icon()
    assert icon is not None and icon[:4] == b"icns"


# --- building --------------------------------------------------------------------------------


def test_a_build_writes_the_plist_the_script_and_the_icon(box: Box) -> None:
    launcher = box.launcher()
    message = launcher.build()

    bundle = box.applications / f"netkeep {paths.data_dir_digest(box.data)}.app"
    assert launcher.path == bundle
    assert message == f"Built {bundle}: it opens netkeep with {box.executable}."
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    assert info == {
        "CFBundleIdentifier": f"fun.tnkr.netkeep.launcher.{paths.data_dir_digest(box.data)}",
        "CFBundleName": "netkeep",
        "CFBundleDisplayName": bundle.stem,
        "CFBundleExecutable": "netkeep",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": "9.8.7",
        "CFBundleVersion": "9.8.7",
        "LSMinimumSystemVersion": "14.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "CFBundleIconFile": "netkeep.icns",
    }
    script = bundle / "Contents" / "MacOS" / "netkeep"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert (bundle / "Contents" / "Resources" / "netkeep.icns").read_bytes()[:4] == b"icns"
    assert (bundle / "Contents" / "PkgInfo").read_bytes() == b"APPL????"
    files = sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file())
    assert files == [
        "Contents/Info.plist",
        "Contents/MacOS/netkeep",
        "Contents/PkgInfo",
        "Contents/Resources/netkeep.icns",
    ]
    # Nothing but the bundle in the Applications folder: no staging folder left.
    assert [p.name for p in box.applications.iterdir()] == [bundle.name]
    assert launcher.up_to_date()


def test_the_script_runs_the_absolute_netkeep_and_nothing_else(box: Box) -> None:
    """The whole script, written out: the data directory, the installed ``netkeep`` by its
    absolute path (``open --launcher``), and the dialog. No other command."""
    launcher = box.launcher()
    launcher.build()
    text = (launcher.path / "Contents" / "MacOS" / "netkeep").read_text()
    quoted = "'" + str(box.executable).replace("'", "'\\''") + "'"
    osascript = f"'{box.bin}/osascript'"
    assert text == (
        "#!/bin/sh\n"
        "# netkeep's launcher app (#754), written by netkeep setup; netkeep update rewrites\n"
        "# it. It opens netkeep for one data directory, or says in a dialog why it can't.\n"
        f"export NETKEEP_DATA='{box.data.resolve()}'\n"
        "export NETKEEP_LOG_STDERR_LEVEL=WARNING\n"
        f"netkeep={quoted}\n"
        'if [ -x "$netkeep" ]; then\n'
        '  message=$("$netkeep" open --launcher 2>&1) && exit 0\n'
        "else\n"
        "  message=\"netkeep isn't installed at $netkeep any more. Install it again with the"
        " installer in netkeep's README, then run netkeep setup in Terminal.\"\n"
        "fi\n"
        "[ -n \"$message\" ] || message='netkeep didn'\\''t open. Open Terminal and run:"
        " netkeep open'\n"
        f"{osascript} -e 'on run argv' -e 'display dialog (item 1 of argv) with title"
        ' "netkeep" buttons {"OK"} default button "OK" with icon caution\' -e \'end run\''
        ' -- "$message" >/dev/null\n'
        "exit 1\n"
    )


def test_the_real_script_names_usr_bin_osascript(box: Box) -> None:
    text = box.launcher(osascript=launcher_app.OSASCRIPT).render_script()
    assert "'/usr/bin/osascript' -e 'on run argv'" in text


def test_the_script_carries_the_login_items_settings(box: Box, tmp_path: Path) -> None:
    config = tmp_path / "conf" / "config.toml"
    environ = {keychain.SERVICE_ENV: "netkeep-scratch", paths.CONFIG_ENV: str(config)}
    text = box.launcher(environ=environ).render_script()
    assert "export NETKEEP_KEYCHAIN_SERVICE='netkeep-scratch'\n" in text
    assert f"export NETKEEP_CONFIG='{config.resolve()}'\n" in text


def test_the_script_takes_the_login_items_settings_from_its_plist(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plist's values win over the builder's environment; the environment counts only
    when there is no plist."""
    plist_config = tmp_path / "plist-conf" / "config.toml"
    shell_config = tmp_path / "shell-conf" / "config.toml"
    item = make_item(
        FakeLaunchctl(),
        tmp_path,
        box.data,
        environ={keychain.SERVICE_ENV: "from-plist", paths.CONFIG_ENV: str(plist_config)},
    )
    monkeypatch.setenv(keychain.SERVICE_ENV, "from-shell")
    monkeypatch.setenv(paths.CONFIG_ENV, str(shell_config))
    launcher = Launcher.for_login_item(item, applications_dir=box.applications)

    before = launcher.render_script()
    assert "export NETKEEP_KEYCHAIN_SERVICE='from-shell'\n" in before
    assert f"export NETKEEP_CONFIG='{shell_config.resolve()}'\n" in before

    item.enable()
    after = launcher.render_script()
    assert "export NETKEEP_KEYCHAIN_SERVICE='from-plist'\n" in after
    assert f"export NETKEEP_CONFIG='{plist_config.resolve()}'\n" in after
    assert "from-shell" not in after and str(shell_config) not in after


def test_a_plist_without_those_settings_exports_none(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_item(FakeLaunchctl(), tmp_path, box.data)
    item.enable()
    monkeypatch.setenv(keychain.SERVICE_ENV, "from-shell")
    text = Launcher.for_login_item(item, applications_dir=box.applications).render_script()
    assert "NETKEEP_KEYCHAIN_SERVICE" not in text and "NETKEEP_CONFIG" not in text


def _write_agent_plist(item: LoginItem, environment: dict[str, object]) -> None:
    """The login item's plist, by hand, with these ``EnvironmentVariables``."""
    item.plist_path.parent.mkdir(parents=True, exist_ok=True)
    item.plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": item.label,
                "ProgramArguments": [str(item.executable), "serve"],
                "EnvironmentVariables": environment,
            }
        )
    )


def _exports(script: str) -> list[str]:
    return [line for line in script.splitlines() if line.startswith("export ")]


def test_only_the_two_named_settings_come_from_the_plist(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever else the plist carries, the script exports its own data directory, the
    fixed stderr level, and only the plist's Keychain service and config."""
    for name in (keychain.SERVICE_ENV, paths.CONFIG_ENV):
        monkeypatch.delenv(name, raising=False)
    item = make_item(FakeLaunchctl(), tmp_path, box.data)
    config = tmp_path / "plist-conf" / "config.toml"
    _write_agent_plist(
        item,
        {
            keychain.SERVICE_ENV: "from-plist",
            paths.CONFIG_ENV: str(config),
            "PATH": "/tmp/evil-bin:/usr/bin",
            "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
            paths.DATA_ENV: str(tmp_path / "another-data"),
            "NETKEEP_LOG_STDERR_LEVEL": "DEBUG",
        },
    )
    script = Launcher.for_login_item(item, applications_dir=box.applications).render_script()
    assert _exports(script) == [
        f"export NETKEEP_DATA='{box.data.resolve()}'",
        "export NETKEEP_LOG_STDERR_LEVEL=WARNING",
        "export NETKEEP_KEYCHAIN_SERVICE='from-plist'",
        f"export NETKEEP_CONFIG='{config.resolve()}'",
    ]
    for leaked in ("evil", "DYLD", "PATH=", "another-data", "DEBUG"):
        assert leaked not in script


def test_a_non_string_plist_value_is_dropped_and_the_script_still_builds(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (keychain.SERVICE_ENV, paths.CONFIG_ENV):
        monkeypatch.delenv(name, raising=False)
    item = make_item(FakeLaunchctl(), tmp_path, box.data)
    _write_agent_plist(item, {keychain.SERVICE_ENV: "from-plist", paths.CONFIG_ENV: 42})
    assert item.plist_environment() == {keychain.SERVICE_ENV: "from-plist"}
    launcher = Launcher.for_login_item(item, applications_dir=box.applications)
    assert _exports(launcher.render_script())[2:] == [
        "export NETKEEP_KEYCHAIN_SERVICE='from-plist'"
    ]


def test_a_malformed_plist_counts_as_none_and_the_launcher_still_builds(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cut-short XML plist raises expat's error, not plistlib's: it reads as no plist,
    so the builder's environment is used, and nothing raises on the way."""
    monkeypatch.setenv(keychain.SERVICE_ENV, "from-shell")
    item = make_item(FakeLaunchctl(), tmp_path, box.data)
    item.plist_path.parent.mkdir(parents=True, exist_ok=True)
    item.plist_path.write_text('<?xml version="1.0"?>\n<plist version="1.0"><dict><key>Label')
    assert item.plist_environment() is None
    launcher = Launcher.for_login_item(item, applications_dir=box.applications)
    assert "export NETKEEP_KEYCHAIN_SERVICE='from-shell'" in _exports(launcher.render_script())
    assert launcher_app.refresh(launcher, build=True) == (
        f"Built {launcher.path}: it opens netkeep with {item.executable}."
    )
    assert item.status().installed  # status reads the same file without raising


def test_a_rebuild_replaces_the_bundle_whole(box: Box) -> None:
    launcher = box.launcher()
    launcher.build()
    stray = launcher.path / "Contents" / "Resources" / "stray.txt"
    stray.write_text("left over")
    assert not launcher.up_to_date()

    message = launcher.build()

    assert message.startswith("Rebuilt ")
    assert not stray.exists()
    assert launcher.up_to_date()
    assert [p.name for p in box.applications.iterdir()] == [launcher.path.name]


def test_a_changed_mode_or_script_is_not_up_to_date(box: Box) -> None:
    launcher = box.launcher()
    launcher.build()
    script = launcher.path / "Contents" / "MacOS" / "netkeep"
    script.chmod(0o644)
    assert not launcher.up_to_date()
    launcher.build()
    script.write_text("#!/bin/sh\n/usr/bin/open -a Calculator\n")
    assert not launcher.up_to_date()


def test_a_missing_icon_builds_without_one(box: Box) -> None:
    launcher = box.launcher(icon=None)
    assert launcher.build() == (
        f"Built {launcher.path} (without its icon): it opens netkeep with {box.executable}."
    )
    info = plistlib.loads((launcher.path / "Contents" / "Info.plist").read_bytes())
    assert "CFBundleIconFile" not in info
    assert not (launcher.path / "Contents" / "Resources").exists()


def test_the_default_data_directory_gets_netkeep_app(box: Box) -> None:
    launcher = box.launcher(data=paths.default_data_dir())
    assert launcher.path == box.applications / "netkeep.app"
    info = plistlib.loads(launcher.render_info_plist(icon=False))
    assert info["CFBundleIdentifier"] == "fun.tnkr.netkeep.launcher"
    assert info["CFBundleDisplayName"] == "netkeep"


def test_another_app_in_the_way_is_never_replaced_or_removed(box: Box) -> None:
    launcher = box.launcher()
    theirs = launcher.path / "Contents"
    theirs.mkdir(parents=True)
    (theirs / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))

    with pytest.raises(LauncherError, match="isn't netkeep's launcher"):
        launcher.build()
    with pytest.raises(LauncherError, match="isn't netkeep's launcher"):
        launcher.remove()
    assert launcher.status().state is State.NOT_OURS
    assert plistlib.loads((theirs / "Info.plist").read_bytes())["CFBundleIdentifier"] == (
        "com.example.other"
    )


def test_a_symbolic_link_is_never_followed(box: Box, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    launcher = box.launcher()
    box.applications.mkdir()
    launcher.path.symlink_to(elsewhere)
    with pytest.raises(LauncherError, match="isn't netkeep's launcher"):
        launcher.build()
    launcher.path.unlink()

    linked = tmp_path / "linked-home" / "Applications"
    linked.parent.mkdir()
    linked.symlink_to(elsewhere)
    with pytest.raises(LauncherError, match="not a plain folder"):
        box.launcher(applications_dir=linked).build()
    assert list(elsewhere.iterdir()) == []


def test_off_macos_nothing_is_built(box: Box) -> None:
    launcher = box.launcher(macos=False)
    assert launcher.status().supported is False
    with pytest.raises(LauncherError, match="macOS"):
        launcher.build()
    assert not box.applications.exists()


def test_no_installed_netkeep_writes_nothing(box: Box) -> None:
    with pytest.raises(LauncherError, match="can't find the installed netkeep"):
        box.launcher(executable=None).build()
    assert not box.applications.exists()


def test_remove_takes_only_the_bundle(box: Box) -> None:
    launcher = box.launcher()
    launcher.build()
    neighbor = box.applications / "Other.app"
    neighbor.mkdir()
    assert launcher.remove() == f"done: removed {launcher.path}."
    assert not launcher.path.exists() and neighbor.is_dir()
    assert launcher.remove() == f"already done: there is no {launcher.path}."


def test_status_says_when_it_points_at_another_netkeep(box: Box, tmp_path: Path) -> None:
    assert box.launcher().status().state is State.MISSING
    box.launcher().build()
    assert box.launcher().status().state is State.BUILT
    assert box.launcher().status().runs == str(box.executable)
    moved = box.launcher(executable=tmp_path / "new" / "netkeep")
    assert moved.status().state is State.STALE
    assert not moved.up_to_date()


def test_refresh_rebuilds_a_stale_one_and_builds_a_missing_one_only_when_told(
    box: Box, tmp_path: Path
) -> None:
    assert launcher_app.refresh(box.launcher(), build=False) is None
    assert not box.applications.exists()
    built = launcher_app.refresh(box.launcher(), build=True)
    assert built is not None and built.startswith("Built ")
    assert launcher_app.refresh(box.launcher(), build=True) is None  # already right
    moved = box.launcher(executable=tmp_path / "new" / "netkeep")
    rebuilt = launcher_app.refresh(moved, build=False)
    assert rebuilt is not None and rebuilt.startswith("Rebuilt ")
    assert moved.status().runs == str(tmp_path / "new" / "netkeep")


@pytest.fixture
def unsearchable(box: Box) -> Iterator[Path]:
    """The scratch Applications folder at mode 000, so even looking for the bundle fails."""
    if os.geteuid() == 0:
        pytest.skip("root ignores folder permissions")
    box.applications.mkdir(parents=True)
    box.applications.chmod(0)
    try:
        yield box.applications
    finally:
        box.applications.chmod(0o755)


def test_refresh_reports_an_unsearchable_folder(box: Box, unsearchable: Path) -> None:
    with pytest.raises(PermissionError):
        box.launcher().status()
    for build in (True, False):
        note = launcher_app.refresh(box.launcher(), build=build)
        assert note is not None and note.startswith("couldn't check or rebuild it: ")
        assert "Permission denied" in note


def test_refresh_reports_a_failure_instead_of_raising(box: Box) -> None:
    launcher = box.launcher()
    launcher.path.mkdir(parents=True)  # not ours: refresh leaves it alone
    assert launcher_app.refresh(launcher, build=True) is None
    shutil.rmtree(launcher.path)
    box.applications.rmdir()
    box.applications.write_text("a file, not a folder")
    note = launcher_app.refresh(launcher, build=True)
    assert note is not None and note.startswith("couldn't check or rebuild it: ")


# --- the script, run -------------------------------------------------------------------------


def test_the_script_runs_netkeep_open_launcher_and_shows_no_dialog_on_success(box: Box) -> None:
    launcher = box.launcher()
    launcher.build()
    code, calls = box.run_script(launcher)
    assert code == 0
    assert calls == [f"netkeep open --launcher data={box.data.resolve()}"]


def test_a_failure_shows_what_netkeep_printed_in_a_dialog(box: Box) -> None:
    box.netkeep('echo "netkeep isn\'t running; run: netkeep setup"\nexit 1\n')
    launcher = box.launcher()
    launcher.build()
    code, calls = box.run_script(launcher)
    assert code == 1
    assert calls[0].startswith("netkeep open --launcher")
    assert calls[1].startswith("osascript [-e][on run argv][-e][display dialog (item 1 of argv)")
    assert calls[1].endswith("[-e][end run][--][netkeep isn't running; run: netkeep setup]")
    assert len(calls) == 2  # never launchctl or open from the script


def test_a_message_that_looks_like_an_option_stays_data(box: Box) -> None:
    """osascript reads ``-e`` after its script lines as more script; ``--`` ends its
    options, so the message is ``item 1 of argv`` whatever it starts with."""
    box.netkeep('echo "-e return 42"\nexit 1\n')
    launcher = box.launcher()
    launcher.build()
    code, calls = box.run_script(launcher)
    assert code == 1
    assert calls[1].endswith("[-e][end run][--][-e return 42]")


def test_what_netkeep_prints_to_stderr_reaches_the_dialog(box: Box) -> None:
    """A refusal on stderr, such as an update in progress, shows its own text."""
    box.netkeep('echo "error: an update is in progress." >&2\nexit 1\n')
    launcher = box.launcher()
    launcher.build()
    code, calls = box.run_script(launcher)
    assert code == 1
    assert calls[1].endswith("[--][error: an update is in progress.]")


def test_a_silent_failure_still_says_what_to_run(box: Box) -> None:
    box.netkeep("exit 1\n")
    launcher = box.launcher()
    launcher.build()
    code, calls = box.run_script(launcher)
    assert code == 1
    assert calls[-1].endswith("[--][netkeep didn't open. Open Terminal and run: netkeep open]")


def test_a_missing_netkeep_says_to_reinstall(box: Box) -> None:
    launcher = box.launcher()
    launcher.build()
    box.executable.unlink()
    code, calls = box.run_script(launcher)
    assert code == 1
    assert len(calls) == 1
    assert f"netkeep isn't installed at {box.executable} any more." in calls[0]


# --- open --launcher: reaching serve ---------------------------------------------------------


class Serve:
    """A health check that answers once launchd has started the agent, and a clock."""

    def __init__(self, launchctl: FakeLaunchctl, label: str) -> None:
        self.launchctl = launchctl
        self.label = label
        self.running = False
        self.lock = False
        self.now = 0.0

    def answers(self, _base: str) -> bool:
        started = (
            any(call[0] in ("kickstart", "bootstrap") for call in self.launchctl.calls)
            and self.label in self.launchctl.loaded
        )
        return self.running or started

    def seams(self) -> ServeSeams:
        return ServeSeams(
            answers=self.answers,
            lock_held=lambda: self.lock,
            sleep=self.sleep,
            clock=lambda: self.now,
        )

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _item(tmp_path: Path, launchctl: FakeLaunchctl) -> LoginItem:
    return LoginItem(
        data=tmp_path / "nk-data",
        agents_dir=tmp_path / "LaunchAgents",
        runner=launchctl,
        uid=UID,
        executable=tmp_path / "bin" / "netkeep",
        macos=True,
        environ={},
        lock_held=lambda: False,
        sleep=lambda _s: None,
    )


@pytest.fixture
def launchctl() -> FakeLaunchctl:
    return FakeLaunchctl()


def _changes(launchctl: FakeLaunchctl) -> list[list[str]]:
    return [call for call in launchctl.calls if call[0] not in ("print", "print-disabled")]


def test_a_running_serve_is_left_alone(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    item = _item(tmp_path, launchctl)
    serve = Serve(launchctl, item.label)
    serve.running = True
    assert launcher_app.reach_serve(item, BASE, serve.seams(), wait_s=60) is None
    assert launchctl.calls == []


def test_a_loaded_but_stopped_job_is_kickstarted(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    item = _item(tmp_path, launchctl)
    item.enable()
    launchctl.calls.clear()  # launchd has the job; its serve has exited
    serve = Serve(launchctl, item.label)

    assert launcher_app.reach_serve(item, BASE, serve.seams(), wait_s=60) is None
    assert _changes(launchctl) == [["kickstart", f"gui/{UID}/{item.label}"]]


def test_a_job_the_update_booted_out_is_bootstrapped_then_kickstarted(
    tmp_path: Path, launchctl: FakeLaunchctl
) -> None:
    """``LoginItem.stop`` (``netkeep update``) removes the job from launchd, where a bare
    ``kickstart`` would fail: the plist's job is added back first."""
    item = _item(tmp_path, launchctl)
    item.enable()
    item.stop()
    assert item.label not in launchctl.loaded
    launchctl.calls.clear()
    serve = Serve(launchctl, item.label)

    assert launcher_app.reach_serve(item, BASE, serve.seams(), wait_s=60) is None
    assert _changes(launchctl) == [
        ["bootstrap", f"gui/{UID}", str(item.plist_path)],
        ["kickstart", f"gui/{UID}/{item.label}"],
    ]


def test_a_login_item_that_is_off_starts_nothing(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    item = _item(tmp_path, launchctl)
    problem = launcher_app.reach_serve(item, BASE, Serve(launchctl, item.label).seams(), wait_s=60)
    assert problem is not None and problem.endswith("Open Terminal and run: netkeep setup")
    assert _changes(launchctl) == []


def test_a_disabled_login_item_starts_nothing(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    item = _item(tmp_path, launchctl)
    item.enable()
    item.stop()
    launchctl.disabled.add(item.label)
    launchctl.calls.clear()
    problem = launcher_app.reach_serve(item, BASE, Serve(launchctl, item.label).seams(), wait_s=60)
    assert problem is not None and "turned off" in problem
    assert _changes(launchctl) == []


def test_a_held_serve_lock_only_waits(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    """A serve holds the lock but doesn't answer yet (say, still migrating): starting the
    agent would only fail against the lock, so the launcher waits."""
    item = _item(tmp_path, launchctl)
    item.enable()
    item.stop()
    launchctl.calls.clear()
    serve = Serve(launchctl, item.label)
    serve.lock = True
    problem = launcher_app.reach_serve(item, BASE, serve.seams(), wait_s=3)
    assert problem is not None
    assert problem.startswith(f"netkeep didn't answer at {BASE} within 3 seconds.")
    assert "serve.err.log may say why" in problem
    assert _changes(launchctl) == []


def test_a_serve_that_never_answers_times_out(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    item = _item(tmp_path, launchctl)
    item.enable()
    item.stop()
    serve = Serve(launchctl, item.label)
    serve.answers = lambda _base: False  # type: ignore[method-assign]
    problem = launcher_app.reach_serve(item, BASE, serve.seams(), wait_s=5)
    assert problem is not None and "within 5 seconds" in problem
    assert serve.now >= 5


def test_launchctl_refusing_says_what_to_run(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    item = _item(tmp_path, launchctl)
    item.enable()
    item.stop()
    launchctl.fail = "bootstrap"
    problem = launcher_app.reach_serve(item, BASE, Serve(launchctl, item.label).seams(), wait_s=5)
    assert problem == (
        "netkeep couldn't start: launchctl bootstrap failed: Input/output error. Open"
        " Terminal and run: netkeep login-item status."
    )


def test_a_kickstart_refused_right_after_bootstrap_still_waits_for_health(
    tmp_path: Path, launchctl: FakeLaunchctl
) -> None:
    """``RunAtLoad`` starts the job at ``bootstrap``; launchd may then refuse the
    ``kickstart``. That isn't a failure: the wait for /health decides."""
    item = _item(tmp_path, launchctl)
    item.enable()
    item.stop()
    launchctl.calls.clear()
    launchctl.fail = "kickstart"
    assert (
        launcher_app.reach_serve(item, BASE, Serve(launchctl, item.label).seams(), wait_s=5) is None
    )
    assert [c[0] for c in _changes(launchctl)] == ["bootstrap", "kickstart"]


def test_a_kickstart_refused_for_a_loaded_job_is_reported(
    tmp_path: Path, launchctl: FakeLaunchctl
) -> None:
    item = _item(tmp_path, launchctl)
    item.enable()
    launchctl.fail = "kickstart"
    serve = Serve(launchctl, item.label)
    serve.answers = lambda _base: False  # type: ignore[method-assign]
    problem = launcher_app.reach_serve(item, BASE, serve.seams(), wait_s=5)
    assert problem is not None
    assert problem.startswith("netkeep couldn't start: launchctl kickstart failed")


def test_a_stale_login_item_is_named_when_it_fails(
    tmp_path: Path, launchctl: FakeLaunchctl
) -> None:
    item = _item(tmp_path, launchctl)
    other = tmp_path / "old-checkout" / "netkeep"
    item.plist_path.parent.mkdir(parents=True, exist_ok=True)
    item.plist_path.write_bytes(
        plistlib.dumps({"Label": item.label, "ProgramArguments": [str(other), "serve"]})
    )
    serve = Serve(launchctl, item.label)
    serve.answers = lambda _base: False  # type: ignore[method-assign]
    problem = launcher_app.reach_serve(item, BASE, serve.seams(), wait_s=1)
    assert problem is not None
    assert f"Its login item runs {other}, not {item.executable}" in problem
    assert "Repair" in problem


def test_start_refuses_when_the_login_item_is_off(tmp_path: Path, launchctl: FakeLaunchctl) -> None:
    with pytest.raises(LoginItemError, match="the login item is off"):
        _item(tmp_path, launchctl).start()
    assert _changes(launchctl) == []


# --- netkeep open --launcher ---------------------------------------------------------------


class FakeChrome:
    def __init__(self, result: LaunchResult) -> None:
        self.result = result
        self.starts: list[tuple[str, str]] = []

    def factory(self, cdp_url: str, base: str, key: str) -> FakeChrome:
        self.starts.append((base, key))
        return self

    async def start(self, *, first_run: bool = False) -> LaunchResult:
        assert not first_run
        return self.result


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[FakeChrome, Serve]]:
    launchctl = FakeLaunchctl()

    def item() -> LoginItem:
        return _item(tmp_path, launchctl)

    serve = Serve(launchctl, item().label)
    chrome = FakeChrome(LaunchResult(LaunchStatus.STARTED, "started it", pid=4242))
    monkeypatch.setattr(login_item, "LoginItem", item)
    monkeypatch.setattr(post_install, "serve_answers", serve.answers)
    monkeypatch.setattr(launcher_app, "_POLL_S", 0.0)
    monkeypatch.setattr(post_install, "SERVE_WAIT_S", 0.0)
    monkeypatch.setattr(cli, "_open_launcher", chrome.factory)
    monkeypatch.setattr(local_api_key, "stored_key", lambda: "fake-local-key")
    yield chrome, serve


def test_open_launcher_starts_the_login_item_then_opens_the_window(
    opened: tuple[FakeChrome, Serve], tmp_path: Path
) -> None:
    chrome, serve = opened
    _item(tmp_path, serve.launchctl).enable()
    _item(tmp_path, serve.launchctl).stop()

    result = CliRunner().invoke(cli.app, ["open", "--launcher"])

    assert result.exit_code == 0, result.output
    assert result.output == ""  # no link printed: there is no terminal
    assert chrome.starts == [("http://127.0.0.1:8000", "fake-local-key")]
    assert ["kickstart", f"gui/{UID}/{serve.label}"] in serve.launchctl.calls


def test_open_launcher_prints_only_the_problem_and_opens_nothing(
    opened: tuple[FakeChrome, Serve],
) -> None:
    chrome, _serve = opened
    result = CliRunner().invoke(cli.app, ["open", "--launcher"])
    assert result.exit_code == 1
    assert result.output == (
        "netkeep isn't running, and it doesn't start at login. Open Terminal and run:"
        " netkeep setup\n"
    )
    assert chrome.starts == []


def test_open_launcher_reports_a_chrome_refusal(opened: tuple[FakeChrome, Serve]) -> None:
    chrome, serve = opened
    serve.running = True
    chrome.result = LaunchResult(
        LaunchStatus.REFUSED, "a run holds the browser", Refusal.RUN_HOLDS_BROWSER
    )
    result = CliRunner().invoke(cli.app, ["open", "--launcher"])
    assert result.exit_code == 1
    assert result.output == "netkeep's Chrome: a run holds the browser.\n"


def test_open_launcher_uses_the_login_items_port(
    opened: tuple[FakeChrome, Serve], tmp_path: Path
) -> None:
    chrome, serve = opened
    serve.running = True
    _item(tmp_path, serve.launchctl).enable(port=8123)
    result = CliRunner().invoke(cli.app, ["open", "--launcher"])
    assert result.exit_code == 0, result.output
    assert chrome.starts == [(BASE, "fake-local-key")]


def test_the_open_flag_is_hidden() -> None:
    result = CliRunner().invoke(cli.app, ["open", "--help"])
    assert "--launcher" not in result.output


def test_version_default_is_the_package_version(box: Box) -> None:
    info = plistlib.loads(box.launcher(version=__version__).render_info_plist(icon=True))
    assert info["CFBundleShortVersionString"] == __version__
