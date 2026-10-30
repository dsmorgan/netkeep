"""scripts/chrome.sh and scripts/reset-data.sh, run through sh.

Nothing here starts a browser or touches a real data directory: chrome.sh only
ever runs with --dry-run or --status, against a temp profile and a port nothing
of ours listens on, and reset-data.sh only against a temp data directory. HOME
points nowhere, so no default path can reach a real one.
"""

from __future__ import annotations

import http.server
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest
from time_limit import Stopwatch

from netkeep import migrations
from netkeep.db import make_engine, make_session_factory
from netkeep.linkedin import activity_lock
from netkeep.models import SettingKV
from netkeep.services import browser_launch, serve_lock
from netkeep.services.backup import AUTO_SEND_ROW_KEY
from netkeep.services.users import ensure_local_user

ROOT = Path(__file__).resolve().parent.parent
CHROME = ROOT / "scripts" / "chrome.sh"
RESET = ROOT / "scripts" / "reset-data.sh"
SH = shutil.which("sh")

#: The system tools a script under test may use, and nothing anyone installed: no
#: Homebrew, no ~/.local/bin, no directory a script under test could write into or that
#: could hand it a real tool where a test meant a shim (#700). /usr/sbin is for lsof.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin"

#: The ports a test must never reach: the maintainer's live Chrome and live netkeep.
LIVE_PORTS = (9222, 8000)

pytestmark = pytest.mark.skipif(SH is None or sys.platform == "win32", reason="needs a POSIX sh")


def _config_cdp_port(config_path: str) -> str | None:
    """The port in a NETKEEP_CONFIG file's ``[linkedin] cdp_url`` -- the
    real key path in netkeep's own config loader (netkeep/config.py's
    ``Settings.linkedin: LinkedInSettings``, ``LinkedInSettings.cdp_url``),
    read with the same ``tomllib`` netkeep itself parses config with,
    not a regex that could be fooled by a commented-out line or a wrong
    section (#183 re-review gap 3c). ``None`` if the file cannot be read, is
    not valid TOML, has no ``cdp_url``, or that URL names no explicit port
    (a config with no ``cdp_url``, or one with no port at all, must not be
    read as "pinned" just because the key was set)."""
    try:
        with open(config_path, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    linkedin = raw.get("linkedin")
    if not isinstance(linkedin, dict):
        return None
    cdp_url = linkedin.get("cdp_url")
    if not isinstance(cdp_url, str):
        return None
    port = urlparse(cdp_url).port
    return None if port is None else str(port)


def _require_pinned_port(
    args: tuple[str, ...], env: dict[str, str], pinned_port: int | str | None
) -> None:
    """Refuse to run chrome.sh in a test unless this call pins its port away
    from 9222 -- the real default, and David's real Chrome's port.

    Falling through to chrome.sh's own DEFAULT_PORT, on a machine where a real
    `netkeep` and a real Chrome both happen to be reachable, reaches that
    real Chrome: a #183 re-review probe did exactly this. ``--port`` is the
    direct way to pin it; ``NETKEEP_CONFIG`` (a test's own ``cdp_url``) is
    another -- read for real, not trusted on sight: a config with no
    ``cdp_url``, or one that still says 9222, must not pass (#183 re-review
    gap). ``pinned_port`` is for a test that controls the port through a fake
    netkeep's own JSON answer instead of either -- it must state the value
    it configured that fake with, so this guard can check it rather than
    trusting the test to have gotten it right silently.

    Every comparison against 9222 here is numeric, not string: "09222" is
    still port 9222 (leading zeros do not change a decimal value), and a
    plain string compare would have let it slip past (#183 re-review gap
    3a). A non-digit value (chrome.sh's own malformed-port test passes one
    on purpose) cannot equal 9222 either way, so it is left for chrome.sh's
    own validation to reject -- this guard is about 9222 specifically, not
    general port syntax.
    """

    def names_9222(value: str) -> bool:
        return value.isdigit() and int(value) == 9222

    literal: str | None = None
    args_list = list(args)
    for i, arg in enumerate(args_list):
        if arg == "--port" and i + 1 < len(args_list):
            literal = args_list[i + 1]
        elif arg.startswith("--port="):
            literal = arg[len("--port=") :]
    if literal is not None:
        assert not names_9222(literal), "a chrome.sh test must pin a port other than 9222"
        return
    if "NETKEEP_CONFIG" in env:
        config_port = _config_cdp_port(env["NETKEEP_CONFIG"])
        if config_port is None:
            raise AssertionError(
                "a chrome.sh test's NETKEEP_CONFIG must set a [linkedin] cdp_url "
                "with an explicit port -- this one had none, or could not be read"
            )
        assert not names_9222(config_port), "a chrome.sh test must pin a port other than 9222"
        return
    if pinned_port is not None:
        assert not names_9222(str(pinned_port)), "a chrome.sh test must pin a port other than 9222"
        return
    raise AssertionError(
        "a chrome.sh invocation in tests must pin a port other than 9222 -- pass "
        "--port, set NETKEEP_CONFIG to a cdp_url with an explicit non-9222 port, "
        "or pass run_sh(..., pinned_port=N) for a fake netkeep's own JSON answer. "
        "Never let a test fall through to the real default: that can reach a real "
        "Chrome on a machine where one is actually running on it."
    )


def _curl_shim(shim_dir: Path) -> Path:
    """A `curl` on PATH that refuses any invocation naming port 9222 or 8000, as a
    backstop that does not depend on a test author remembering to pin one
    (#183 re-review gap): `_require_pinned_port` only catches what a test
    *says* it will do; this catches what chrome.sh actually tries to connect
    to, no matter how it got there. The real curl's path is resolved before
    the shim is built, since by the time it runs, PATH has this directory
    in front of it.

    The match is numeric, not a literal ":9222" substring: a `sed` extraction
    pulls the digit run after a colon, skipping any leading zeros, so "09222"
    is still caught and a genuinely different port like "92220" is not
    falsely flagged just for starting with the same four digits (#183
    re-review gap 3a).

    On a refusal, the shim also drops a marker file in ``shim_dir`` (returned
    here): `run_sh` checks for it after the run and raises if it is there, so
    a 9222 that reached curl despite everything else fails the test loudly --
    chrome.sh's own `cdp_version()` swallows a failed curl silently, treating
    it as "nothing answered", so nothing else would surface this (#183
    re-review gap 3b).
    """
    real = shutil.which("curl", path=SYSTEM_PATH)
    assert real is not None, "curl must be on the system PATH for the shim to fall back to"
    marker = shim_dir / "REFUSED_LIVE_PORT"
    shim = shim_dir / "curl"
    refused = " || ".join(f'[ "$port" -eq {port} ]' for port in LIVE_PORTS)
    shim.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        "  port=$(printf '%s' \"$arg\" | sed -n 's/.*:0*\\([0-9][0-9]*\\).*/\\1/p')\n"
        f'  if [ -n "$port" ] && {{ {refused}; }} 2>/dev/null; then\n'
        f"    : > {str(marker)!r}\n"
        '    echo "refused: $port" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    return marker


_run_sh_tmp: Path | None = None


@pytest.fixture(autouse=True)
def _run_sh_in_tmp_path(tmp_path: Path) -> Iterator[None]:
    """Give run_sh this test's tmp_path, for its curl shim and every install location
    it points the script at (#700): nothing a script under test writes lands outside."""
    global _run_sh_tmp
    _run_sh_tmp = tmp_path / "run-sh"
    _run_sh_tmp.mkdir()
    try:
        yield
    finally:
        _run_sh_tmp = None


def _real_path_snapshot() -> dict[str, frozenset[tuple[str, int]]]:
    """Each directory on this process's own PATH: its entries and their mtimes.

    Only run_sh's own runs are checked; the two tests that drive a script through Popen
    (to answer its prompt, or to interrupt it) are not. A ``brew`` or ``uv tool`` run
    elsewhere during a test changes a PATH directory too, and fails that test falsely:
    re-run it alone."""
    snapshot: dict[str, frozenset[tuple[str, int]]] = {}
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or directory in snapshot:
            continue
        try:
            with os.scandir(directory) as entries:
                snapshot[directory] = frozenset(
                    (entry.name, entry.stat(follow_symlinks=False).st_mtime_ns) for entry in entries
                )
        except OSError:
            snapshot[directory] = frozenset()
    return snapshot


def _run_confined(
    command: list[str], env: dict[str, str], stdin: str | None
) -> subprocess.CompletedProcess[str]:
    """Run ``command``, then fail if anything on the real PATH changed meanwhile."""
    before = _real_path_snapshot()
    result = subprocess.run(
        command, capture_output=True, text=True, env=env, input=stdin, timeout=60
    )
    after = _real_path_snapshot()
    changed = sorted(d for d in before.keys() | after.keys() if before.get(d) != after.get(d))
    assert not changed, f"a script under test changed a directory on the real PATH: {changed}"
    return result


def run_sh(
    script: Path,
    *args: str,
    env: dict[str, str],
    stdin: str | None = None,
    pinned_port: int | str | None = None,
    allow_no_curl: bool = False,
) -> subprocess.CompletedProcess[str]:
    """``allow_no_curl`` skips the curl-shim backstop below, for the one test
    that deliberately runs with no curl at all, to pin chrome.sh's own
    ``command -v curl`` check (C9): the shim's fallback always finds a real
    curl, so leaving it in would quietly hand that test a working curl and
    defeat its own point. Asserted, not just trusted (#183 re-review gap
    3d): a future PATH built for some other reason could leave curl
    reachable after all, silently dropping the backstop for that call
    without allow_no_curl's own point (a test with no curl on PATH on
    purpose) actually requiring it to be absent."""
    assert SH is not None
    assert _run_sh_tmp is not None, "run_sh runs only inside a test (it needs tmp_path)"
    if script.name == "chrome.sh":
        _require_pinned_port(args, env, pinned_port)
    # The interpreter's own bin first: chrome.sh and reset-data.sh find netkeep as
    # <repo>/.venv/bin or on PATH, and a sibling worktree has no .venv of its own (#210).
    # Then the system's tools and nothing else (#700).
    path = os.pathsep.join((str(Path(sys.executable).parent), SYSTEM_PATH))
    # Every install location points into tmp_path, and HOME at a folder there that
    # does not exist (as /nonexistent did): a test that needs a home makes its own.
    confined = {
        name: str(_run_sh_tmp / name.lower())
        for name in (
            "UV_INSTALL_DIR",
            "UV_TOOL_BIN_DIR",
            "UV_TOOL_DIR",
            "CARGO_HOME",
            "XDG_BIN_HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "TMPDIR",
        )
    }
    Path(confined["TMPDIR"]).mkdir(exist_ok=True)
    full_env = {"PATH": path, "HOME": str(_run_sh_tmp / "home"), **confined, **env}
    if allow_no_curl:
        assert shutil.which("curl", path=full_env.get("PATH", "")) is None, (
            "allow_no_curl=True but curl is still reachable on this PATH -- either "
            "make it genuinely absent, or drop the flag and let the shim run"
        )
        return _run_confined([SH, str(script), *args], full_env, stdin)
    # A curl shim ahead of whatever PATH was just built -- even a test's own
    # custom one, which otherwise would have replaced the line above entirely:
    # the backstop for #183's guard above, so a port 9222 that reached curl
    # despite it is refused too, whether or not a test caller remembered to
    # pin one. lsof and ps are read-only (they report on an existing
    # listener; they never open a connection to it themselves), so they need
    # no shim. The shim lives in tmp_path (#700), one per call.
    shim_dir = Path(tempfile.mkdtemp(prefix="curl-shim-", dir=_run_sh_tmp))
    marker = _curl_shim(shim_dir)
    full_env["PATH"] = os.pathsep.join((str(shim_dir), full_env.get("PATH", "")))
    result = _run_confined([SH, str(script), *args], full_env, stdin)
    if marker.exists():
        raise AssertionError(
            f"{script.name} tried to reach port 9222 or 8000 despite the pinned-port guard"
            " -- the curl shim refused it; see stderr for 'refused: <port>'"
        )
    return result


def chrome_flags(line: str) -> list[str]:
    return [f for f in re.findall(r"(?<!\S)--[a-z-]+", line) if f != "--args"]


def isolated_chrome_script(tmp_path: Path) -> Path:
    """A copy of chrome.sh under a fake repo root with no ``.venv`` of its own.

    chrome.sh always prefers ``<repo>/.venv/bin/netkeep`` over anything on
    PATH, and this repo's own ``.venv`` exists once ``make install`` has run
    -- so a test of the PATH/``command -v netkeep`` branch (a `uv tool`/pipx
    install, #183 review should-fix 2) has to run the script from somewhere
    that branch actually gets a chance to run, not the real checkout.
    """
    repo = tmp_path / "isolated-repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    copy = scripts / "chrome.sh"
    copy.write_text(CHROME.read_text())
    copy.chmod(0o755)
    return copy


def fake_netkeep_cli(
    path: Path,
    *,
    port: int | str,
    profile: Path,
    remote: str | None = None,
    shebang: str | None = None,
) -> None:
    """A stand-in `netkeep` whose `browser launch --json` answers like the real
    CLI's, with a real python shebang -- enough for chrome.sh to ask it for the
    port and profile the way it would ask the genuine one. ``port`` may be a
    non-numeric string, to simulate a malformed answer. ``shebang`` defaults to
    a direct absolute-path interpreter (what `uv sync`/pip normally write);
    pass e.g. ``"/usr/bin/env python3"`` for an env-style entry point instead.
    """
    path.write_text(
        f"#!{shebang or sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:4] == ['browser', 'launch', '--json']:\n"
        "    print(json.dumps({\n"
        f"        'cdp_url': 'http://127.0.0.1:{port}',\n"
        f"        'port': {port!r},\n"
        f"        'profile': {str(profile)!r},\n"
        f"        'remote': {remote!r},\n"
        "    }))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def fake_netkeep_cli_sh_shim(path: Path, *, port: int, profile: Path) -> None:
    """A stand-in `netkeep` whose entry point is itself a ``#!/bin/sh``
    relaunch shim (what `uv tool`'s distlib-style launcher uses) rather than
    python directly. The JSON it prints must still get parsed by a *real*
    python found separately -- never by handing the json.load snippet to this
    shell itself (#183 re-review should-fix 2).
    """
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = browser ] && [ "$2" = launch ] && [ "$3" = --json ]; then\n'
        f'  printf \'{{"cdp_url": "http://127.0.0.1:{port}", "port": {port},'
        f' "profile": "{profile}", "remote": null}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    path.chmod(0o755)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@contextmanager
def process_with_args(*args: str) -> Iterator[int]:
    """A live `sh` whose command line carries ``args``, the way Chrome's carries its flags."""
    # `; :` keeps sh from exec-ing sleep, so the sh -- and its arguments -- stay in ps.
    proc = subprocess.Popen(["sh", "-c", "sleep 60; :", "holder", *args], start_new_session=True)
    try:
        yield proc.pid
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@contextmanager
def process_named(directory: Path, name: str) -> Iterator[int]:
    """A live process whose executable path is ``directory/name``."""
    sleep = shutil.which("sleep")
    assert sleep is not None
    # A symlink, not a copy: macOS kills a copied system binary, and ps shows
    # the path it was started by either way.
    exe = directory / name
    exe.symlink_to(sleep)
    proc = subprocess.Popen([str(exe), "60"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@contextmanager
def process_named_holding_open(
    directory: Path, name: str, held: Path, *extra_args: str
) -> Iterator[int]:
    """A live process named ``directory/name`` (``ps -o comm=`` reports the symlink's
    own name, not the interpreter it points to) that keeps ``held`` open until killed.
    ``extra_args`` land after the ``-c`` script on the command line (as plain
    positional arguments the script itself ignores), standing in for whatever
    flags a real Chrome's command line would carry.

    What tells an actual holder of a profile apart from a pid that merely matches
    by name or number (#183 review bug 1): ``lock_holder()`` now requires the
    locking pid to have a file open under the profile, not just to be alive and
    Chrome-named.
    """
    held.parent.mkdir(parents=True, exist_ok=True)
    held.touch(exist_ok=True)
    exe = directory / name
    exe.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(exe), "-c", f"f = open({str(held)!r}); import time; time.sleep(60)", *extra_args]
    )
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@contextmanager
def fake_cdp_endpoint() -> Iterator[int]:
    """A loopback server answering /json/version the way Chrome's does."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"Browser": "Chrome/1.0.0.0"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def start(profile: Path, *extra: str, port: int | None = None) -> subprocess.CompletedProcess[str]:
    return run_sh(
        CHROME,
        "--dry-run",
        "--port",
        str(port if port is not None else free_port()),
        "--profile-dir",
        str(profile),
        *extra,
        env={},
    )


# --- chrome.sh -------------------------------------------------------------------


def test_run_sh_refuses_an_unpinned_chrome_invocation() -> None:
    """Safety gap: the harness itself must refuse to run chrome.sh without an
    explicit, non-9222 port pinned some way -- a re-review probe reached
    David's real Chrome by falling through to chrome.sh's own default."""
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={})


def test_run_sh_refuses_an_explicit_port_9222() -> None:
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", "--port", "9222", env={})


def test_run_sh_refuses_pinned_port_9222() -> None:
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={}, pinned_port=9222)


def test_run_sh_accepts_netkeep_config_with_no_explicit_port(tmp_path: Path) -> None:
    """NETKEEP_CONFIG alone satisfies the guard: the test's own cdp_url pins
    the port, even with no --port flag on the command line."""
    config = tmp_path / "config.toml"
    config.write_text(f'[linkedin]\ncdp_url = "http://127.0.0.1:{free_port()}"\n')
    run_sh(CHROME, "--dry-run", env={"NETKEEP_CONFIG": str(config)})


def test_run_sh_refuses_netkeep_config_with_no_cdp_url(tmp_path: Path) -> None:
    """#183 re-review gap: a NETKEEP_CONFIG with no cdp_url at all must not
    be read as pinning anything -- it falls through to chrome.sh's own 9222
    default exactly as surely as having no NETKEEP_CONFIG would."""
    config = tmp_path / "config.toml"
    config.write_text("[linkedin]\n")
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEP_CONFIG": str(config)})


def test_run_sh_refuses_netkeep_config_with_an_unreadable_path(tmp_path: Path) -> None:
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEP_CONFIG": str(tmp_path / "missing.toml")})


def test_run_sh_refuses_netkeep_config_with_port_9222(tmp_path: Path) -> None:
    """#183 re-review gap: a cdp_url that still says 9222 must not pass either."""
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://127.0.0.1:9222"\n')
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEP_CONFIG": str(config)})


def test_run_sh_refuses_netkeep_config_with_no_port_in_the_url(tmp_path: Path) -> None:
    """#183 re-review gap: a cdp_url naming no explicit port at all must not
    pass either -- there is nothing here to confirm isn't 9222."""
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://127.0.0.1"\n')
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEP_CONFIG": str(config)})


def test_run_sh_refuses_a_leading_zero_port_9222() -> None:
    """#183 re-review gap 3a: "09222" is still port 9222 numerically; a plain
    string compare against the literal "9222" would have let it slip past."""
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", "--port", "09222", env={})


def test_run_sh_refuses_netkeep_config_with_a_leading_zero_port(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://127.0.0.1:09222"\n')
    with pytest.raises(AssertionError):
        run_sh(CHROME, "--status", env={"NETKEEP_CONFIG": str(config)})


def test_run_sh_accepts_pinned_port_with_a_leading_zero_that_is_not_9222(tmp_path: Path) -> None:
    """A leading zero on some other port is not a 9222 bypass attempt; the
    numeric check must not over-fire on it."""
    run_sh(
        CHROME,
        "--dry-run",
        "--port",
        "01234",
        "--profile-dir",
        str(tmp_path / "profile"),
        env={},
    )


def test_curl_shim_refuses_a_9222_argument_without_a_network_call(tmp_path: Path) -> None:
    """#183 re-review gap: the curl-shim backstop must refuse a :9222 URL by
    inspecting its arguments alone, never by attempting the request first and
    failing some other way. A non-routable address (RFC 5737) proves it: if
    the shim fell through to a real request, this would hang or time out
    instead of refusing at once."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _curl_shim(shim_dir)
    watch = Stopwatch()  # without garbage collection (#472)
    result = subprocess.run(
        [str(shim_dir / "curl"), "-fsS", "--max-time", "2", "http://192.0.2.1:9222/json/version"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    elapsed = watch.elapsed
    assert result.returncode != 0
    assert "refused: 9222" in result.stderr
    assert elapsed < 1, "the shim must refuse instantly, not attempt the request first"


def test_curl_shim_passes_through_anything_else(tmp_path: Path) -> None:
    """The shim must still work as curl for every other argument."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _curl_shim(shim_dir)
    result = subprocess.run(
        [str(shim_dir / "curl"), "--version"], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    assert "curl" in result.stdout.lower()


def test_curl_shim_refuses_a_leading_zero_9222(tmp_path: Path) -> None:
    """#183 re-review gap 3a: ":09222" is still port 9222 numerically; a plain
    "*:9222*" substring match would have missed it entirely."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _curl_shim(shim_dir)
    result = subprocess.run(
        [str(shim_dir / "curl"), "--max-time", "2", "http://192.0.2.1:09222/json/version"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "refused: 9222" in result.stderr


def test_curl_shim_does_not_falsely_flag_a_different_port_sharing_the_digits(
    tmp_path: Path,
) -> None:
    """#183 re-review gap 3a: a port like 92220 is not 9222 -- the match must
    require a non-digit or the end of the argument right after it, not just
    the substring ":9222" anywhere."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _curl_shim(shim_dir)
    result = subprocess.run(
        [str(shim_dir / "curl"), "-fsS", "--max-time", "1", "http://192.0.2.1:92220/x"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert "refused: 9222" not in result.stderr


def test_run_sh_raises_if_chrome_sh_still_reaches_9222_via_curl(tmp_path: Path) -> None:
    """#183 re-review gap 3b: even when a test's own --port/NETKEEP_CONFIG/
    pinned_port satisfies the guard, run_sh must still notice and fail loudly
    if chrome.sh's own curl call reached 9222 anyway. chrome.sh's
    cdp_version() swallows a failed curl silently (`|| true`), treating it as
    "nothing answered", so nothing else would surface this -- the marker
    file the shim drops on a refusal is what run_sh checks for instead."""
    fake_chrome = tmp_path / "chrome.sh"
    fake_chrome.write_text("#!/bin/sh\ncurl -fsS http://127.0.0.1:9222/json/version\nexit 0\n")
    fake_chrome.chmod(0o755)
    with pytest.raises(AssertionError, match="9222"):
        run_sh(fake_chrome, "--port", "1234", env={})


def test_curl_shim_refuses_port_8000_too(tmp_path: Path) -> None:
    """I19 (#700): the maintainer's live netkeep listens on 8000; the shim refuses it
    as it does Chrome's 9222, from the arguments alone (RFC 5737 address)."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _curl_shim(shim_dir)
    result = subprocess.run(
        [str(shim_dir / "curl"), "-fsS", "--max-time", "2", "http://192.0.2.1:8000/api/v1/me"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "refused: 8000" in result.stderr


def test_run_sh_raises_if_a_script_reaches_8000_via_curl(tmp_path: Path) -> None:
    fake = tmp_path / "fake.sh"
    fake.write_text("#!/bin/sh\ncurl -fsS http://192.0.2.1:8000/api/v1/me\nexit 0\n")
    fake.chmod(0o755)
    with pytest.raises(AssertionError, match="8000"):
        run_sh(fake, env={})


def test_run_sh_confines_path_and_install_locations(tmp_path: Path) -> None:
    """I19 (#700): the script sees its curl shim (in tmp_path), the interpreter's bin and
    the system's tools, nothing else on PATH, and every install location in tmp_path."""
    names = ("HOME", "UV_INSTALL_DIR", "UV_TOOL_BIN_DIR", "CARGO_HOME", "XDG_DATA_HOME")
    fake = tmp_path / "env.sh"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$PATH\"\n"
        + "".join(f"printf '%s\\n' \"${name}\"\n" for name in names)
    )
    fake.chmod(0o755)
    result = run_sh(fake, env={})
    assert result.returncode == 0, result.stderr
    path, *locations = result.stdout.splitlines()
    shim, interpreter_bin, *system = path.split(os.pathsep)
    assert Path(shim).is_relative_to(tmp_path)
    assert interpreter_bin == str(Path(sys.executable).parent)
    assert os.pathsep.join(system) == SYSTEM_PATH
    assert len(locations) == len(names)
    assert all(Path(location).is_relative_to(tmp_path) for location in locations)


def test_run_sh_fails_when_a_script_writes_onto_the_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I19 (#700): what the 2026-10-19 fake uv in /usr/local/bin would have tripped. The
    "real" PATH here starts with a folder in tmp_path, so the write lands nowhere real."""
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    monkeypatch.setenv("PATH", f"{real_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    fake = tmp_path / "installer.sh"
    fake.write_text(f"#!/bin/sh\n: > {str(real_bin / 'uv')!r}\n")
    fake.chmod(0o755)
    with pytest.raises(AssertionError, match="changed a directory on the real PATH"):
        run_sh(fake, env={})


def test_run_sh_allow_no_curl_asserts_curl_is_actually_absent(tmp_path: Path) -> None:
    """#183 re-review gap 3d: allow_no_curl must not be trusted blindly -- if
    curl is still reachable on the given PATH, that silently drops the
    backstop for that call instead of genuinely testing "no curl at all"."""
    real_curl = shutil.which("curl")
    assert real_curl is not None
    with pytest.raises(AssertionError):
        run_sh(
            CHROME,
            "--dry-run",
            "--port",
            str(free_port()),
            env={"PATH": str(Path(real_curl).parent)},
            allow_no_curl=True,
        )


def test_chrome_script_constants_match_the_cli() -> None:
    """The fallback profile name and port are the ones `browser launch` uses."""
    text = CHROME.read_text()
    assert f'PROFILE_DIRNAME="{browser_launch.CHROME_PROFILE_DIRNAME}"' in text
    assert f"DEFAULT_PORT={browser_launch.DEFAULT_CDP_PORT}" in text


def test_chrome_script_launches_with_exactly_the_flags_the_cli_prints(tmp_path: Path) -> None:
    port = free_port()
    profile = tmp_path.resolve() / "profile with space"
    result = start(profile, port=port)
    assert result.returncode == 0, result.stderr
    launch = [line for line in result.stdout.splitlines() if "remote-debugging-port" in line]
    assert len(launch) == 1
    printed = " ".join(browser_launch.chrome_launch_command(port, profile))
    # Every Chrome flag, with or without a value, so an extra one (a
    # fingerprint change) fails too; `--args` is `open`'s, not Chrome's.
    assert (
        chrome_flags(launch[0])
        == chrome_flags(printed)
        # #733: the two first-run flags are the only additions.
        == [
            "--remote-debugging-port",
            "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir",
        ]
    )
    assert f"--remote-debugging-port={port}" in launch[0]
    assert f"--user-data-dir={profile}" in launch[0]
    assert not profile.exists(), "a dry run created the profile"


def test_chrome_script_takes_port_and_profile_from_netkeeps_config(tmp_path: Path) -> None:
    port = free_port()
    config = tmp_path / "config.toml"
    config.write_text(f'[linkedin]\ncdp_url = "http://127.0.0.1:{port}"\n')
    result = run_sh(
        CHROME,
        "--status",
        env={"NETKEEP_DATA": str(tmp_path), "NETKEEP_CONFIG": str(config)},
    )
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    profile = tmp_path.resolve() / browser_launch.CHROME_PROFILE_DIRNAME
    assert f"profile  {profile}" in result.stdout
    assert f"port     {port}: nothing is listening" in result.stdout


def test_chrome_script_refuses_a_cdp_url_on_another_host(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    # 192.0.2.0/24 (RFC 5737) is non-routable: this test is about the host
    # mismatch, not the port, so the port is deliberately not 9222 anyway (the
    # run_sh guard below requires that of every NETKEEP_CONFIG it is handed).
    config.write_text('[linkedin]\ncdp_url = "http://192.0.2.10:9333"\n')
    result = run_sh(
        CHROME,
        "--dry-run",
        env={"NETKEEP_DATA": str(tmp_path), "NETKEEP_CONFIG": str(config)},
    )
    assert result.returncode == 1
    assert "192.0.2.10" in result.stderr


def test_chrome_script_finds_port_and_profile_through_a_symlinked_netkeep(
    tmp_path: Path,
) -> None:
    """#183 review should-fix 2: a `uv tool`/pipx install puts a symlink on
    PATH with no interpreter beside it. Reading python from the entry point's
    own shebang (which `head` follows straight through the symlink) must still
    work, with no `<repo>/.venv` around to fall back on."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    profile = tmp_path.resolve() / "profile"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeep_cli(real_dir / "netkeep", port=port, profile=profile)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeep").symlink_to(real_dir / "netkeep")  # no python beside the symlink
    path = f"{bin_dir}:{SYSTEM_PATH}"
    result = run_sh(script, "--status", env={"PATH": path}, pinned_port=port)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert f"profile  {profile}" in result.stdout
    assert f"port     {port}: nothing is listening" in result.stdout


def test_chrome_script_resolves_an_env_shebang_entry_point(tmp_path: Path) -> None:
    """#183 re-review should-fix 2: an `#!/usr/bin/env python3` entry point
    (not a direct absolute-path shebang) must be resolved through PATH and
    used to parse the JSON -- honoring whatever custom profile it answers
    with, the same as a direct-shebang entry point would."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    profile = tmp_path.resolve() / "a custom profile"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeep_cli(
        real_dir / "netkeep", port=port, profile=profile, shebang="/usr/bin/env python3"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeep").symlink_to(real_dir / "netkeep")
    # python3 must resolve through PATH for /usr/bin/env to find it; the venv
    # this suite runs under always has one beside `python` (uv/pip's doing).
    python_dir = Path(sys.executable).parent
    assert (python_dir / "python3").exists()
    path = f"{bin_dir}:{python_dir}:{SYSTEM_PATH}"
    result = run_sh(script, "--status", env={"PATH": path}, pinned_port=port)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert f"profile  {profile}" in result.stdout


def test_chrome_script_resolves_an_env_dash_s_shebang_entry_point(tmp_path: Path) -> None:
    """#183 re-review nit 5: `#!/usr/bin/env -S python3-custom -I` names
    python3-custom, not `-S` (env's own split-string option) -- the first
    word of the shebang's remainder that is not itself an option. A
    deliberately unusual interpreter name, on a PATH with nothing else
    python-shaped on it at all (not even a plain python3): taking `-S` at
    face value would find no interpreter whatsoever and fall back to
    "no Python was found", not silently succeed some other way."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    profile = tmp_path.resolve() / "profile"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeep_cli(
        real_dir / "netkeep",
        port=port,
        profile=profile,
        shebang="/usr/bin/env -S python3-custom -I",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "curl",
        "lsof",
        "ps",
        "awk",
        "sed",
        "uname",
        "mkdir",
        "basename",
        "dirname",
        "tr",
        "rm",
        "cat",
        "kill",
        "grep",
        "sh",
        "head",
        "mktemp",
    ):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    (bin_dir / "netkeep").symlink_to(real_dir / "netkeep")
    (bin_dir / "python3-custom").symlink_to(sys.executable)
    # Nothing generically python-shaped is on this PATH -- only chrome.sh's
    # other prerequisites and the shebang's own unusual interpreter name.
    result = run_sh(script, "--status", env={"PATH": str(bin_dir)}, pinned_port=port)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert f"profile  {profile}" in result.stdout


def test_chrome_script_falls_back_to_python3_for_a_sh_shim_entry_point(tmp_path: Path) -> None:
    """#183 re-review should-fix 2: an entry point that is itself a
    ``#!/bin/sh`` relaunch shim (uv tool's distlib-style launcher) must never
    be handed the json.load snippet -- that is not python, and running it
    under sh would just fail or garble. It falls back to plain `python3` on
    PATH to parse the answer instead."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    profile = tmp_path.resolve() / "profile"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeep_cli_sh_shim(real_dir / "netkeep", port=port, profile=profile)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeep").symlink_to(real_dir / "netkeep")
    python_dir = Path(sys.executable).parent
    path = f"{bin_dir}:{python_dir}:{SYSTEM_PATH}"
    result = run_sh(script, "--status", env={"PATH": path}, pinned_port=port)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert f"profile  {profile}" in result.stdout


def test_chrome_script_warns_when_no_python_can_parse_a_sh_shim_answer(tmp_path: Path) -> None:
    """#183 re-review should-fix 2: when netkeep answered but no python could
    be found to parse it, the warning must say so specifically, not the
    generic "could not ask netkeep" (which means something else: it was
    never asked at all, or never answered)."""
    script = isolated_chrome_script(tmp_path)
    port = free_port()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeep_cli_sh_shim(
        real_dir / "netkeep", port=port, profile=tmp_path.resolve() / "profile"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeep").symlink_to(real_dir / "netkeep")
    # Everything chrome.sh needs except any python: no python3 for it to fall
    # back to.
    minimal_bin = tmp_path / "minimal-bin"
    minimal_bin.mkdir()
    for tool in (
        "sh",
        "curl",
        "lsof",
        "ps",
        "awk",
        "sed",
        "uname",
        "mkdir",
        "basename",
        "dirname",
        "tr",
        "rm",
        "cat",
        "kill",
        "grep",
        "head",
        "mktemp",
    ):
        found = shutil.which(tool)
        if found:
            (minimal_bin / tool).symlink_to(found)
    path = f"{bin_dir}:{minimal_bin}"
    result = run_sh(script, "--dry-run", "--port", str(free_port()), env={"PATH": path})
    assert "netkeep answered but no Python was found to parse it" in result.stderr
    # With no profile named, it never guesses the default data folder (#661).
    assert result.returncode == 1
    assert "pass --profile or --data-dir" in result.stderr


def test_chrome_script_never_uses_the_new_default_before_the_move(tmp_path: Path) -> None:
    """A profile in .../netkeep while .../netkeeper waits to move is refused (#661),
    whoever named it: --data-dir, NETKEEP_DATA, or a netkeep that answered so."""
    script = isolated_chrome_script(tmp_path)
    home = tmp_path / "home"
    support = home / "Library" / "Application Support"
    (support / "netkeeper").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_netkeep_cli(
        bin_dir / "netkeep", port=free_port(), profile=support / "netkeep" / "chrome-profile"
    )
    path = f"{bin_dir}:{SYSTEM_PATH}"
    result = run_sh(
        script,
        "--dry-run",
        "--port",
        str(free_port()),
        env={"PATH": path, "HOME": str(home), "NETKEEP_DATA": str(support / "netkeep")},
    )
    assert result.returncode == 1
    assert "run netkeep migrate first" in result.stderr
    assert not (support / "netkeep").exists()
    # After the move (the marker there), the same profile is fine.
    (support / "netkeep").mkdir()
    (support / "netkeep" / "migrated-from-netkeeper.json").write_text("{}")
    again = run_sh(
        script,
        "--dry-run",
        "--port",
        str(free_port()),
        env={"PATH": path, "HOME": str(home), "NETKEEP_DATA": str(support / "netkeep")},
    )
    assert "run netkeep migrate first" not in again.stderr


def test_chrome_script_stops_with_netkeeps_message_and_creates_no_folder(tmp_path: Path) -> None:
    """A failing ``browser launch --json`` (a refused migration, #661) stops the script
    with netkeep's own message; it never falls back to the default data folder."""
    script = isolated_chrome_script(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "netkeep"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stderr.write(\"error: netkeep can't move netkeeper's data yet: both exist\\n\")\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    path = f"{bin_dir}:{SYSTEM_PATH}"
    result = run_sh(script, "--port", str(free_port()), env={"PATH": path, "HOME": str(home)})
    assert result.returncode == 1
    assert "can't move netkeeper's data yet: both exist" in result.stderr
    assert list(home.rglob("*")) == []


def test_chrome_script_round_trips_a_non_ascii_profile_path(tmp_path: Path) -> None:
    """#183 review should-fix 2: sed parsing the JSON directly mangled a
    profile path containing a quote, a backslash, or non-ASCII text (json.dumps
    escapes the last as \\uXXXX) -- a real parser must round-trip it intact."""
    port = free_port()
    config = tmp_path / "config.toml"
    config.write_text(f'[linkedin]\ncdp_url = "http://127.0.0.1:{port}"\n')
    data_dir = tmp_path / "José's Área"
    result = run_sh(
        CHROME,
        "--status",
        env={"NETKEEP_DATA": str(data_dir), "NETKEEP_CONFIG": str(config)},
    )
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    profile = data_dir.resolve() / browser_launch.CHROME_PROFILE_DIRNAME
    assert f"profile  {profile}" in result.stdout


def test_chrome_script_refuses_a_non_numeric_port_from_json(tmp_path: Path) -> None:
    """#183 review nit 4: with a real parser, a malformed answer's port must
    still be checked before reaching curl/lsof, not just --port's."""
    script = isolated_chrome_script(tmp_path)
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    fake_netkeep_cli(
        real_dir / "netkeep", port="not-a-port", profile=tmp_path.resolve() / "profile"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "netkeep").symlink_to(real_dir / "netkeep")
    path = f"{bin_dir}:{SYSTEM_PATH}"
    result = run_sh(script, "--dry-run", env={"PATH": path}, pinned_port="not-a-port")
    assert result.returncode == 1
    assert "must be a number" in result.stderr


def test_chrome_script_normalizes_the_profile_spelling(tmp_path: Path) -> None:
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    result = start(Path(f"{profile}//"))
    assert result.returncode == 0, result.stderr
    assert f"--user-data-dir={profile}" in result.stdout
    assert f"--user-data-dir={profile}/" not in result.stdout


def test_chrome_script_clears_a_stale_lock_only_when_nothing_holds_the_profile(
    tmp_path: Path,
) -> None:
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).symlink_to("otherhost-999999")
    result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert f"would: rm {profile / name}" in result.stdout
        assert (profile / name).is_symlink(), "a dry run removed the lock"


def test_chrome_script_refuses_when_a_chrome_holds_the_profile_under_another_spelling(
    tmp_path: Path,
) -> None:
    """The #182 review's reproduction: a trailing slash hid a running Chrome."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("thishost-999999")
    with process_with_args(f"--user-data-dir={profile}"):
        result = start(Path(f"{profile}/"))
    assert result.returncode == 1
    assert "running on this profile" in result.stderr
    assert (profile / "SingletonLock").is_symlink()


def test_chrome_script_trusts_the_locks_pid_over_the_command_line(tmp_path: Path) -> None:
    """A Chrome started with a spelling the script cannot match still holds its lock,
    as long as it genuinely has the profile open. Found only through the lock
    file, not a command-line match, so the message names it (#183 review bug 1)."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named_holding_open(tmp_path, "Google Chrome", profile / "held") as pid:
        (profile / "SingletonLock").symlink_to(f"otherhost-{pid}")
        result = start(profile)
    assert result.returncode == 1
    assert f"pid {pid}" in result.stderr
    assert f"(per {profile}/SingletonLock)" in result.stderr
    # #183 re-review nit 4: an escape hatch for when the heuristics are wrong.
    assert f"If pid {pid} isn't netkeep's Chrome, remove {profile}/SingletonLock." in result.stderr
    assert (profile / "SingletonLock").is_symlink()


def test_chrome_script_does_not_count_a_file_open_in_a_profile_whose_name_merely_starts_the_same(
    tmp_path: Path,
) -> None:
    """Pins the trailing slash in lock_holder()'s lsof check: a file open under
    chrome-profile-old must not count as chrome-profile itself being open."""
    profile = tmp_path.resolve() / "chrome-profile"
    profile.mkdir()
    sibling = tmp_path.resolve() / "chrome-profile-old"
    with process_named_holding_open(tmp_path, "Google Chrome", sibling / "held") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_accepts_a_holder_whose_own_flag_canonicalizes_to_the_profile(
    tmp_path: Path,
) -> None:
    """#183 review nit 3: a holder's own --user-data-dir is read and
    canonicalized the same way $profile is, so a spelling profile_pids()'s
    plain substring match cannot catch (here, a symlinked alias) is still
    recognized -- without needing lsof to show anything open under it."""
    real_profile = tmp_path.resolve() / "real-profile"
    real_profile.mkdir()
    alias_profile = tmp_path.resolve() / "alias-profile"
    alias_profile.symlink_to(real_profile)
    exe = tmp_path / "Google Chrome"
    exe.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(exe), "-c", "import time; time.sleep(60)", f"--user-data-dir={alias_profile}"]
    )
    try:
        (real_profile / "SingletonLock").symlink_to(f"thishost-{proc.pid}")
        result = start(real_profile)
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 1
    assert f"pid {proc.pid}" in result.stderr


def test_chrome_script_treats_lock_holder_as_present_when_lsof_answers_nothing(
    tmp_path: Path,
) -> None:
    """#183 review nit 3: lsof returning nothing at all for a live, Chrome-named,
    non-helper pid (a startup race, or no permission to inspect it) is not proof
    there is no holder; lock_holder() must fail closed, not clear the lock."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named(tmp_path, "Google Chrome") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "lsof"
        real = shutil.which("lsof")
        assert real is not None
        # Silent on -Fn (the lock-holder check); real lsof for port_listener's
        # -iTCP check, so the rest of the script still runs normally.
        shim.write_text(f'#!/bin/sh\ncase "$*" in *-Fn*) exit 0 ;; esac\nexec "{real}" "$@"\n')
        shim.chmod(0o755)
        path = f"{bin_dir}:{SYSTEM_PATH}"
        result = run_sh(
            CHROME,
            "--dry-run",
            "--port",
            str(free_port()),
            "--profile-dir",
            str(profile),
            env={"PATH": path},
        )
    assert result.returncode == 1
    assert f"pid {pid}" in result.stderr


def test_chrome_script_fails_closed_when_flag_is_mismatched_and_lsof_is_empty(
    tmp_path: Path,
) -> None:
    """#183 re-review round 3 should-fix 1: a mismatched --user-data-dir is
    not, on its own, proof this pid is not the holder -- a relative flag, or
    one mangled by a positional argument (round 2's should-fix 1), looks
    exactly like a genuine mismatch for a real holder too. Only lsof actually
    answering and finding nothing under the profile may call a lock stale;
    lsof answering nothing at all must fail closed exactly as when there was
    no flag to go on in the first place, mismatch or not."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    other = tmp_path.resolve() / "other-profile"
    exe = tmp_path / "Google Chrome"
    exe.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(exe), "-c", "import time; time.sleep(60)", f"--user-data-dir={other}"]
    )
    try:
        (profile / "SingletonLock").symlink_to(f"thishost-{proc.pid}")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "lsof"
        real = shutil.which("lsof")
        assert real is not None
        # Empty on -Fn, as if nothing were open -- the fail-closed case this
        # must still trigger despite the mismatched flag.
        shim.write_text(f'#!/bin/sh\ncase "$*" in *-Fn*) exit 0 ;; esac\nexec "{real}" "$@"\n')
        shim.chmod(0o755)
        path = f"{bin_dir}:{SYSTEM_PATH}"
        result = run_sh(
            CHROME,
            "--dry-run",
            "--port",
            str(free_port()),
            "--profile-dir",
            str(profile),
            env={"PATH": path},
        )
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 1
    assert f"pid {proc.pid}" in result.stderr


def test_chrome_script_still_refuses_a_live_holder_behind_a_symlinked_flag_and_a_url(
    tmp_path: Path,
) -> None:
    """#183 re-review should-fix 1 (a regression nit 3 itself introduced): a
    symlinked --user-data-dir alias followed by a positional URL
    (`--user-data-dir=alias https://example.com`) leaves nothing after it for
    `${tail%% --*}` to trim on, so the extracted value is the alias *and* the
    URL mashed together -- which canonicalizes to neither. lsof showing the
    profile genuinely open must still catch this as the holder it is, rather
    than reading the broken-looking flag as proof it is not."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    alias = tmp_path.resolve() / "alias"
    alias.symlink_to(profile)
    with process_named_holding_open(
        tmp_path,
        "Google Chrome",
        profile / "held",
        f"--user-data-dir={alias}",
        "https://example.com",
    ) as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 1
    assert f"pid {pid}" in result.stderr


def test_chrome_script_still_refuses_a_live_holder_behind_a_relative_flag(
    tmp_path: Path,
) -> None:
    """#183 re-review should-fix 1: a relative --user-data-dir canonicalizes
    against this script's own cwd, not whatever the real Chrome's was, so it
    reads as a mismatch even when the pid is genuinely this profile's holder.
    lsof showing the profile open must still catch it."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named_holding_open(
        tmp_path, "Google Chrome", profile / "held", "--user-data-dir=some-relative-name"
    ) as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 1
    assert f"pid {pid}" in result.stderr


def test_chrome_script_refuses_a_lock_whose_pid_was_reused_by_an_unrelated_chrome(
    tmp_path: Path,
) -> None:
    """#183 review bug 1: a lock naming a pid Chrome once held, since reused by an
    entirely different Chrome (your everyday browser, not this profile's), must not
    be mistaken for this profile's holder -- it is treated as stale instead."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with process_named_holding_open(tmp_path, "Google Chrome", elsewhere / "held") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_skips_a_reused_pid_that_is_now_a_helper_process(tmp_path: Path) -> None:
    """#183 review bug 1: a `--type=` helper (renderer, GPU, utility) never holds
    SingletonLock itself; a reused pid landing on one is not this profile's Chrome,
    even with the profile's own files open for some unrelated reason."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    exe = tmp_path / "Google Chrome Helper"
    exe.symlink_to(sys.executable)
    held = profile / "held"
    held.touch()
    proc = subprocess.Popen(
        [str(exe), "-c", f"f = open({str(held)!r}); import time; time.sleep(60)", "--type=renderer"]
    )
    try:
        (profile / "SingletonLock").symlink_to(f"thishost-{proc.pid}")
        result = start(profile)
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_treats_a_lock_naming_a_dead_pid_as_stale(tmp_path: Path) -> None:
    """A lock naming a pid that is simply not running at all -- not merely some
    other, live, non-Chrome process -- must also read as stale: the ``kill -0``
    check, not only the executable-name check that follows it."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    proc = subprocess.Popen(["sh", "-c", "exit 0"])
    proc.wait()
    dead_pid = proc.pid
    (profile / "SingletonLock").symlink_to(f"thishost-{dead_pid}")
    result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_reports_a_holder_on_a_different_port(tmp_path: Path) -> None:
    """#183 review bug 2: a Chrome on this profile but a different debugging port
    has one, just not this one -- "without the debugging port" is the wrong
    message for it."""
    profile = tmp_path.resolve() / "profile"
    with process_with_args("--remote-debugging-port=9333", f"--user-data-dir={profile}") as pid:
        result = start(profile, port=free_port())
    assert result.returncode == 1
    assert "on port 9333" in result.stderr
    assert "without the debugging port" not in result.stderr
    # #183 re-review nit 4: an escape hatch for when the heuristics are wrong.
    assert f"If pid {pid} isn't netkeep's Chrome, remove {profile}/SingletonLock." in result.stderr


def _bin_dir_without(tmp_path: Path, missing: str) -> Path:
    """A PATH containing everything chrome.sh needs except ``missing``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "curl",
        "lsof",
        "ps",
        "awk",
        "sed",
        "uname",
        "mkdir",
        "basename",
        "dirname",
        "tr",
        "rm",
        "cat",
        "kill",
        "grep",
        "sh",
    ):
        if tool == missing:
            continue
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    return bin_dir


def test_chrome_script_refuses_without_curl(tmp_path: Path) -> None:
    """#183 review C9: curl's own tool-requirement check, exercised on its own."""
    bin_dir = _bin_dir_without(tmp_path, "curl")
    result = run_sh(
        CHROME,
        "--dry-run",
        "--port",
        str(free_port()),
        "--profile-dir",
        str(tmp_path / "profile"),
        env={"PATH": str(bin_dir)},
        allow_no_curl=True,
    )
    assert result.returncode == 1
    assert "curl is needed" in result.stderr


def test_chrome_script_refuses_without_lsof(tmp_path: Path) -> None:
    """#183 review C15: lsof's own tool-requirement check, exercised on its own."""
    bin_dir = _bin_dir_without(tmp_path, "lsof")
    result = run_sh(
        CHROME,
        "--dry-run",
        "--port",
        str(free_port()),
        "--profile-dir",
        str(tmp_path / "profile"),
        env={"PATH": str(bin_dir)},
    )
    assert result.returncode == 1
    assert "lsof is needed" in result.stderr


def test_chrome_script_treats_a_lock_naming_a_live_non_chrome_process_as_stale(
    tmp_path: Path,
) -> None:
    """A crashed Chrome's pid can be reused by anything; that is not a holder."""
    profile = tmp_path.resolve() / "profile"
    profile.mkdir()
    with process_named(tmp_path, "sleep") as pid:
        (profile / "SingletonLock").symlink_to(f"thishost-{pid}")
        result = start(profile)
    assert result.returncode == 0, result.stderr
    assert "stale profile lock" in result.stdout


def test_chrome_script_does_not_count_a_profile_whose_path_merely_starts_the_same(
    tmp_path: Path,
) -> None:
    profile = tmp_path.resolve() / "profile"
    with process_with_args(f"--user-data-dir={profile}-old"):
        result = run_sh(
            CHROME, "--status", "--port", str(free_port()), "--profile-dir", str(profile), env={}
        )
    assert result.returncode == 0, result.stderr
    assert "not running on this profile" in result.stdout


def test_chrome_script_refuses_a_port_that_answers_for_another_profile(tmp_path: Path) -> None:
    with fake_cdp_endpoint() as port:
        result = start(tmp_path.resolve() / "profile", port=port)
    assert result.returncode == 1
    assert "not on this profile" in result.stderr


def test_chrome_script_refuses_a_port_that_is_not_a_number(tmp_path: Path) -> None:
    result = run_sh(CHROME, "--dry-run", "--port", "9222x", "--profile-dir", str(tmp_path), env={})
    assert result.returncode == 1
    assert "must be a number" in result.stderr


def test_chrome_script_refuses_a_port_something_else_holds(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        result = start(tmp_path.resolve(), port=s.getsockname()[1])
    assert result.returncode == 1
    assert "not a debuggable Chrome" in result.stderr


# --- reset-data.sh ---------------------------------------------------------------

# reset-data.sh archives and verifies with the sqlite3 command-line tool, which a
# fresh cloud container lacks (#231). There these tests skip with the fix in the
# reason. CI (GitHub Actions sets CI=true) always has the tool, so they never skip
# there: a missing sqlite3 in CI fails rather than quietly leaving the script untested.
needs_sqlite3_cli = pytest.mark.skipif(
    shutil.which("sqlite3") is None and not os.environ.get("CI"),
    reason="needs the sqlite3 command-line tool: apt-get install sqlite3",
)


_db_template: Path | None = None

#: A --restore that reaches netkeep restore starts the netkeep CLI: about 2 s of CPU,
#: and more than 10 s on a loaded machine. ``slow`` gives it the longer limit.
slow_netkeep_restore = pytest.mark.slow


@pytest.fixture(scope="session")
def netkeep_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated netkeep database with the local user, made once: --restore hands the
    write to netkeep restore, which takes only a netkeep database (#700)."""
    work = tmp_path_factory.mktemp("netkeep-db-template")
    engine = make_engine(f"sqlite:///{work / 'migrated.sqlite3'}")
    try:
        migrations.upgrade(engine)
        with make_session_factory(engine)() as session:
            ensure_local_user(session)
            session.commit()
    finally:
        engine.dispose()
    template = work / "template.sqlite3"
    # One self-contained file in rollback-journal mode, with no sidecars to copy.
    with closing(sqlite3.connect(work / "migrated.sqlite3")) as conn:
        conn.execute("VACUUM INTO ?", (str(template),))
    with closing(sqlite3.connect(template)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    return template


@pytest.fixture
def data(tmp_path: Path, netkeep_db_template: Path) -> Iterator[Path]:
    """A data directory with a space in it, like the macOS default."""
    global _db_template
    path = tmp_path / "Application Support"
    path.mkdir()
    _db_template = netkeep_db_template
    try:
        yield path
    finally:
        _db_template = None


def make_db(data: Path, value: int = 42) -> Path:
    """A netkeep database in ``data`` with a ``marker`` table holding ``value``."""
    assert _db_template is not None, "make_db needs the data fixture"
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    shutil.copyfile(_db_template, db)
    # `with sqlite3.connect(...)` commits but never closes, and an open handle is
    # exactly what reset-data.sh refuses to run under.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("create table marker (x integer)")
        conn.execute("insert into marker values (?)", (value,))
    return db


def marker(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute("select x from marker").fetchone()
    return int(row[0])


@needs_sqlite3_cli
def test_reset_dry_run_changes_nothing(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), "--dry-run", env={})
    assert result.returncode == 0, result.stderr
    assert db.exists()
    assert not (data / "archives").exists()


@needs_sqlite3_cli
def test_reset_never_works_on_the_new_default_before_the_move(tmp_path: Path) -> None:
    """Restore's mkdir -p must not create .../netkeep while .../netkeeper waits (#661),
    nor bring .../netkeeper back once it has moved."""
    home = tmp_path / "home"
    support = home / "Library" / "Application Support"
    (support / "netkeeper").mkdir(parents=True)
    archive = tmp_path / "a.sqlite3"
    archive.write_bytes(b"")
    result = run_sh(RESET, "--restore", str(archive), "--yes", env={"HOME": str(home)})
    assert result.returncode == 1
    assert "run netkeep migrate first" in result.stderr
    assert not (support / "netkeep").exists()

    (support / "netkeeper").rmdir()
    (support / "netkeep").mkdir()
    (support / "netkeep" / "migrated-from-netkeeper.json").write_text("{}")
    back = run_sh(
        RESET,
        "--restore",
        str(archive),
        "--yes",
        env={"HOME": str(home), "NETKEEPER_DATA": str(support / "netkeeper")},
    )
    assert back.returncode == 1
    assert "data moved to" in back.stderr
    assert not (support / "netkeeper").exists()


@pytest.mark.parametrize("mode", [["--restore", "a.sqlite3"], [], ["--list"]])
def test_reset_refuses_a_missing_old_folder_without_the_marker(
    tmp_path: Path, mode: list[str]
) -> None:
    """--data-dir naming .../netkeeper after it is gone is refused even with no marker
    (#712): netkeep restore would follow it to .../netkeep, past the in-use check."""
    home = tmp_path / "home"
    support = home / "Library" / "Application Support"
    (support / "netkeep").mkdir(parents=True)
    archive = tmp_path / "a.sqlite3"
    archive.write_bytes(b"")
    args = [str(archive) if arg == "a.sqlite3" else arg for arg in mode]
    result = run_sh(
        RESET,
        *args,
        "--data-dir",
        str(support / "netkeeper"),
        "--yes",
        env={"HOME": str(home)},
    )
    assert result.returncode == 1
    assert "netkeeper doesn't exist; netkeep's data folder is" in result.stderr
    assert not (support / "netkeeper").exists()
    assert not (support / "netkeep" / "archives").exists()


@slow_netkeep_restore
def test_reset_archives_then_removes_and_restore_puts_it_back(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout
    assert not db.exists()
    archives = sorted((data / "archives").iterdir())
    assert len(archives) == 1
    assert marker(archives[0]) == 42

    listed = run_sh(RESET, "--data-dir", str(data), "--list", env={})
    assert listed.returncode == 0, listed.stderr
    # One row, whole: a space in the data directory must not split the path.
    (row,) = listed.stdout.splitlines()
    assert row.startswith(f"{archives[0].name}  ")
    assert "No such file" not in listed.stderr

    restored = run_sh(
        RESET, "--data-dir", str(data), "--restore", archives[0].name, "--yes", env={}
    )
    assert restored.returncode == 0, restored.stderr
    assert marker(db) == 42


@needs_sqlite3_cli
def test_reset_refuses_while_the_database_is_open(data: Path) -> None:
    db = make_db(data)
    with closing(sqlite3.connect(db)) as holder:
        holder.execute("select 1").fetchone()
        result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    assert result.returncode == 1
    assert "open in another process" in result.stderr
    assert marker(db) == 42
    assert not (data / "archives").exists()


def test_reset_refuses_without_lsof_rather_than_assume_the_database_is_free(
    data: Path, tmp_path: Path
) -> None:
    make_db(data)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "sqlite3",
        "sed",
        "awk",
        "date",
        "mktemp",
        "wc",
        "tr",
        "sort",
        "basename",
        "dirname",
        "cp",
        "rm",
        "mkdir",
        "ps",
        "grep",
        "cat",
        "sh",
    ):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": str(bin_dir)})
    assert result.returncode == 1
    assert "lsof is needed" in result.stderr
    assert (data / "netkeeper.sqlite3").exists()


@needs_sqlite3_cli
def test_reset_keeps_the_database_when_the_archive_does_not_verify(
    data: Path, tmp_path: Path
) -> None:
    """An archive that is not a readable database must stop the delete."""
    db = make_db(data)
    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    # A VACUUM INTO that claims success and writes nothing.
    shim.write_text(f'#!/bin/sh\ncase "$2" in VACUUM*) exit 0 ;; esac\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    path = f"{shim_dir}:{SYSTEM_PATH}"
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker(db) == 42


@needs_sqlite3_cli
def test_reset_answering_no_changes_nothing(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--data-dir", str(data), env={}, stdin="n\n")
    assert result.returncode == 0, result.stderr
    assert "nothing changed" in result.stdout
    assert db.exists()
    assert not (data / "archives").exists()


@slow_netkeep_restore
@needs_sqlite3_cli
def test_restore_archives_the_database_in_place_first(data: Path) -> None:
    db = make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (first,) = (data / "archives").iterdir()
    make_db(data, value=2)
    restored = run_sh(RESET, "--data-dir", str(data), "--restore", first.name, "--yes", env={})
    assert restored.returncode == 0, restored.stderr
    assert marker(db) == 1
    archived = sorted(p for p in (data / "archives").iterdir() if p != first)
    assert [marker(p) for p in archived] == [2]


@slow_netkeep_restore
@needs_sqlite3_cli
def test_two_archives_in_one_second_never_share_a_name(data: Path, tmp_path: Path) -> None:
    """The #182 review's reproduction: the second restore overwrote the first archive.

    The stamp is pinned, so all three archives land in one second however long netkeep
    restore takes. Its own pre-restore backups (backups/) are removed between runs:
    netkeep restore refuses a second one in the same second, which is not this test's."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _date_shim(shim_dir, "20260101T000000Z")
    env = {"PATH": f"{shim_dir}:{Path(sys.executable).parent}:{SYSTEM_PATH}"}
    db = make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env=env)
    (source,) = (data / "archives").iterdir()
    for value in (2, 3):
        make_db(data, value=value)
        shutil.rmtree(data / "backups", ignore_errors=True)
        result = run_sh(RESET, "--data-dir", str(data), "--restore", source.name, "--yes", env=env)
        assert result.returncode == 0, result.stderr
    kept = sorted(marker(p) for p in (data / "archives").iterdir())
    assert kept == [1, 2, 3]
    assert marker(db) == 1


def _auto_send_rows(db: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT key, value FROM settings_kv WHERE key = ?", (AUTO_SEND_ROW_KEY,)
        ).fetchall()


@slow_netkeep_restore
@needs_sqlite3_cli
def test_restore_turns_settings_page_auto_send_off(data: Path) -> None:
    """B12 (#700): an archive taken while Settings-page auto-send was on must come back
    with it off, as netkeep restore leaves it (#598). A plain cp brought it back on."""
    db = make_db(data, value=7)
    engine = make_engine(f"sqlite:///{db}")
    try:
        with make_session_factory(engine)() as session:
            user = ensure_local_user(session)
            session.add(SettingKV(user_id=user.id, key=AUTO_SEND_ROW_KEY, value=True))
            session.commit()
    finally:
        engine.dispose()
    assert _auto_send_rows(db) == [(AUTO_SEND_ROW_KEY, "true")]
    archived = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    assert archived.returncode == 0, archived.stderr
    (archive,) = (data / "archives").iterdir()

    restored = run_sh(RESET, "--data-dir", str(data), "--restore", archive.name, "--yes", env={})
    assert restored.returncode == 0, restored.stderr
    assert marker(db) == 7
    assert _auto_send_rows(db) == []
    assert "it is off now" in restored.stdout
    # The archive keeps what it held: only the restored copy was cleared.
    assert _auto_send_rows(archive) == [(AUTO_SEND_ROW_KEY, "true")]


@slow_netkeep_restore
@needs_sqlite3_cli
def test_restore_refuses_while_serve_holds_its_lock(data: Path) -> None:
    """B12 (#700): netkeep restore's serve lock, which the plain cp never checked."""
    make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (archive,) = (data / "archives").iterdir()
    db = make_db(data, value=2)
    held = activity_lock.try_claim_at(data / "locks" / serve_lock.SERVE_LOCK_FILENAME, "serve")
    assert held is not None
    try:
        result = run_sh(RESET, "--data-dir", str(data), "--restore", archive.name, "--yes", env={})
    finally:
        held.release()
    assert result.returncode == 1
    assert "netkeep serve is running" in result.stderr
    assert marker(db) == 2


@slow_netkeep_restore
@needs_sqlite3_cli
def test_restore_refuses_a_migration_this_netkeep_does_not_know(data: Path, tmp_path: Path) -> None:
    """B12 (#700): netkeep restore's revision check, which the plain cp never made."""
    newer = tmp_path / "newer.sqlite3"
    shutil.copyfile(make_db(data, value=9), newer)
    with closing(sqlite3.connect(newer)) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
    db = make_db(data, value=3)
    result = run_sh(RESET, "--data-dir", str(data), "--restore", str(newer), "--yes", env={})
    assert result.returncode == 1
    assert "this netkeep does not know" in result.stderr
    assert marker(db) == 3


@needs_sqlite3_cli
def test_restore_dry_run_names_netkeep_restore_and_changes_nothing(data: Path) -> None:
    make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (archive,) = (data / "archives").iterdir()
    result = run_sh(RESET, "--data-dir", str(data), "--restore", archive.name, "--dry-run", env={})
    assert result.returncode == 0, result.stderr
    assert " restore " in result.stdout
    assert not (data / "netkeeper.sqlite3").exists()
    assert [p.name for p in (data / "archives").iterdir()] == [archive.name]


def _isolated_reset_script(tmp_path: Path, *, netkeep: str | None) -> Path:
    """A copy of reset-data.sh under a fake repo root, whose ``.venv/bin/netkeep`` is
    ``netkeep`` (a sh script body), or missing when that is None."""
    repo = tmp_path / "isolated-repo"
    (repo / "scripts").mkdir(parents=True)
    copy = repo / "scripts" / "reset-data.sh"
    copy.write_text(RESET.read_text())
    copy.chmod(0o755)
    if netkeep is not None:
        bin_dir = repo / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        fake = bin_dir / "netkeep"
        fake.write_text(netkeep)
        fake.chmod(0o755)
    return copy


#: A netkeep whose restore is a plain copy: an old install, before #598's clear.
_COPYING_NETKEEP = (
    '#!/bin/sh\n[ "$1" = restore ] && exec cp -- "$2" "$NETKEEP_DATA/netkeeper.sqlite3"\n'
)


@needs_sqlite3_cli
def test_restore_never_falls_back_to_a_netkeep_on_path(data: Path, tmp_path: Path) -> None:
    """#700 review: with no netkeep in the checkout's venv, --restore stops; it never
    runs whatever netkeep PATH has, which may not clear auto-send."""
    make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (archive,) = (data / "archives").iterdir()
    db = make_db(data, value=2)
    script = _isolated_reset_script(tmp_path, netkeep=None)
    on_path = tmp_path / "path-bin"
    on_path.mkdir()
    (on_path / "netkeep").write_text(_COPYING_NETKEEP)
    (on_path / "netkeep").chmod(0o755)
    env = {"PATH": f"{on_path}:{SYSTEM_PATH}"}
    result = run_sh(script, "--data-dir", str(data), "--restore", str(archive), "--yes", env=env)
    assert result.returncode == 1
    assert "netkeep is needed to restore" in result.stderr
    assert "make install" in result.stderr
    assert marker(db) == 2
    assert [p.name for p in (data / "archives").iterdir()] == [archive.name]


@needs_sqlite3_cli
def test_restore_fails_closed_when_auto_send_survives(data: Path, tmp_path: Path) -> None:
    """#700 review: whatever netkeep ran, the script checks the restored database itself
    and stops loudly when the Settings-page auto-send row is still there."""
    db = make_db(data, value=5)
    with closing(sqlite3.connect(db)) as conn, conn:
        (user_id,) = conn.execute("SELECT id FROM users").fetchone()
        conn.execute(
            "INSERT INTO settings_kv (user_id, key, value, created_at, updated_at)"
            " VALUES (?, ?, 'true', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (user_id, AUTO_SEND_ROW_KEY),
        )
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (archive,) = (data / "archives").iterdir()
    script = _isolated_reset_script(tmp_path, netkeep=_COPYING_NETKEEP)
    result = run_sh(script, "--data-dir", str(data), "--restore", str(archive), "--yes", env={})
    assert result.returncode == 1
    assert "still has the Settings-page LinkedIn auto-send" in result.stderr
    assert "Don't start netkeep serve" in result.stderr


@needs_sqlite3_cli
def test_restore_failure_says_the_database_was_left_as_it_was(data: Path, tmp_path: Path) -> None:
    make_db(data, value=1)
    run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    (archive,) = (data / "archives").iterdir()
    db = make_db(data, value=2)
    script = _isolated_reset_script(tmp_path, netkeep="#!/bin/sh\nexit 1\n")
    result = run_sh(script, "--data-dir", str(data), "--restore", str(archive), "--yes", env={})
    assert result.returncode == 1
    assert "was left as it was" in result.stderr
    assert str(data / "archives") in result.stderr
    assert marker(db) == 2


@needs_sqlite3_cli
def test_restore_refuses_a_file_that_is_not_a_database(data: Path, tmp_path: Path) -> None:
    db = make_db(data)
    junk = tmp_path / "junk.sqlite3"
    junk.write_text("not a database")
    result = run_sh(RESET, "--data-dir", str(data), "--restore", str(junk), "--yes", env={})
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker(db) == 42


@needs_sqlite3_cli
def test_reset_refuses_when_the_server_uses_another_database(data: Path) -> None:
    db = make_db(data)
    result = run_sh(
        RESET,
        "--data-dir",
        str(data),
        "--yes",
        env={"NETKEEP_DATABASE_URL": "postgresql://localhost/netkeep"},
    )
    assert result.returncode == 1
    assert "NETKEEP_DATABASE_URL" in result.stderr
    assert db.exists()


@needs_sqlite3_cli
def test_reset_defaults_to_netkeep_data(data: Path) -> None:
    db = make_db(data)
    result = run_sh(RESET, "--dry-run", env={"NETKEEP_DATA": str(data)})
    assert result.returncode == 0, result.stderr
    assert str(db) in result.stdout


def _lsof_shim(tmp_path: Path, script: str) -> str:
    """A PATH with ``script`` standing in for lsof, the rest of PATH behind it."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "lsof"
    shim.write_text(script)
    shim.chmod(0o755)
    return f"{shim_dir}:{SYSTEM_PATH}"


def _date_shim(shim_dir: Path, stamp: str) -> None:
    """A `date` that answers reset-data.sh's own ``date -u +%Y%m%dT%H%M%SZ``
    with a fixed ``stamp``, so a test knows the exact candidate name in
    advance instead of racing the real clock across a second boundary."""
    real = shutil.which("date")
    assert real is not None
    shim = shim_dir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$*" = "-u +%Y%m%dT%H%M%SZ" ]; then\n'
        f"  printf '%s\\n' {stamp!r}\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)


@needs_sqlite3_cli
def test_reset_refuses_on_a_real_lsof_error(data: Path, tmp_path: Path) -> None:
    """R2: an actual lsof error -- not a missing binary, not a benign warning --
    must still refuse the reset rather than read as "nothing holds it"."""
    make_db(data)
    path = _lsof_shim(tmp_path, "#!/bin/sh\nprintf 'lsof: permission denied\\n' >&2\nexit 1\n")
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "lsof could not check the database" in result.stderr
    assert (data / "netkeeper.sqlite3").exists()


@needs_sqlite3_cli
def test_reset_ignores_a_benign_lsof_warning(data: Path, tmp_path: Path) -> None:
    """#183 review bug 7: an unrelated WARNING on lsof's stderr (a stale network
    mount it scans along the way) must not block a reset when nothing actually
    holds the database. Real warnings run to two or three lines -- the
    continuation lines matter too, not just the first."""
    db = make_db(data)
    real = shutil.which("lsof")
    assert real is not None
    path = _lsof_shim(
        tmp_path,
        "#!/bin/sh\n"
        "printf 'lsof: WARNING: can'\"'\"'t stat() nfs file system /mnt/stale\\n"
        "      Output information may be incomplete.\\n"
        '      assuming "dev=1234" from mount table\\n\' >&2\n'
        f'exec "{real}" "$@"\n',
    )
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 0, result.stderr
    assert not db.exists()


@needs_sqlite3_cli
def test_reset_refuses_when_only_the_wal_sidecar_is_held_open(data: Path) -> None:
    """R3: a holder on ``-wal`` alone, with no fd on the main file, must still
    refuse the reset."""
    db = make_db(data)
    wal = Path(f"{db}-wal")
    wal.write_bytes(b"")
    proc = subprocess.Popen(
        [sys.executable, "-c", f"f = open({str(wal)!r}, 'rb'); import time; time.sleep(30)"]
    )
    try:
        time.sleep(0.3)  # let the interpreter actually get to the open() call
        result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 1
    assert "open in another process" in result.stderr
    assert marker(db) == 42


@needs_sqlite3_cli
def test_reset_rechecks_after_the_confirmation_prompt(data: Path) -> None:
    """R4: a server that starts while the prompt is waiting on an answer must
    still block the reset -- the check made again after it, not only the one
    made before the prompt was shown."""
    assert SH is not None
    db = make_db(data)
    env = {"PATH": SYSTEM_PATH, "HOME": str(data.parent / "no-home")}
    proc = subprocess.Popen(
        [SH, str(RESET), "--data-dir", str(data)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        time.sleep(0.3)  # the prompt is up, waiting on stdin
        with closing(sqlite3.connect(db)) as holder:
            holder.execute("select 1").fetchone()
            assert proc.stdin is not None
            proc.stdin.write("y\n")
            proc.stdin.flush()
            stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 1, stdout
    assert "open in another process" in stderr
    assert marker(db) == 42
    assert not (data / "archives").exists()


def test_reset_script_verifies_the_archive_read_only() -> None:
    """R6/R7: pin the literal `-readonly` flag on both of ``verified()``'s sqlite3
    calls -- dropping either would let a corrupt archive be (re)written to
    instead of failing, or silently create an empty file for a path that does
    not exist instead of reporting it unreadable."""
    text = RESET.read_text()
    assert text.count("sqlite3 -readonly") == 2


@needs_sqlite3_cli
def test_reset_renames_a_failed_archive_instead_of_leaving_it_behind(
    data: Path, tmp_path: Path
) -> None:
    """#183 review bug 5: an archive that was created but does not verify must not
    sit in archives/ looking like a good one; it is renamed ``.failed`` instead."""
    db = make_db(data)
    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  VACUUM*)\n"
        "    target=$(printf '%s' \"$2\" | sed -n \"s/^VACUUM INTO '\\\\(.*\\\\)'\\$/\\\\1/p\")\n"
        "    printf 'not a real database' > \"$target\"\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    path = f"{shim_dir}:{SYSTEM_PATH}"
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "quick_check" in result.stderr
    assert marker(db) == 42
    entries = sorted(p.name for p in (data / "archives").iterdir())
    assert len(entries) == 1
    assert entries[0].endswith(".failed")


@needs_sqlite3_cli
def test_reset_quarantines_a_partial_archive_when_the_raw_copy_fails(
    data: Path, tmp_path: Path
) -> None:
    """Review should-fix 1: when the raw-copy fallback fails partway (an
    unreadable -wal), the partial archive must not sit in archives/ looking
    like a normal one, and must not appear in --list either."""
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker (x integer)")
        conn.execute("INSERT INTO marker VALUES (42)")
    wal = Path(f"{db}-wal")
    wal.touch(exist_ok=True)
    wal.chmod(0o000)
    try:
        real = shutil.which("sqlite3")
        assert real is not None
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "sqlite3"
        shim.write_text(f'#!/bin/sh\ncase "$2" in VACUUM*) exit 1 ;; esac\nexec "{real}" "$@"\n')
        shim.chmod(0o755)
        path = f"{shim_dir}:{SYSTEM_PATH}"
        result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
        assert result.returncode == 1
        assert "could not copy" in result.stderr

        archives = data / "archives"
        assert not list(archives.glob("*.sqlite3")), "a partial archive looks like a good one"
        assert list(archives.glob("*.sqlite3.failed")), "the partial copy should be quarantined"

        listed = run_sh(RESET, "--data-dir", str(data), "--list", env={})
        assert listed.returncode == 0, listed.stderr
        assert "no archives in" in listed.stdout
    finally:
        wal.chmod(0o644)  # so the temp directory's own teardown can remove it


@needs_sqlite3_cli
def test_reset_does_not_clobber_a_concurrent_runs_archive(data: Path, tmp_path: Path) -> None:
    """Re-review should-fix 1: a second reset landing on the same stamp must not
    let this run's raw-copy fallback or quarantine step delete or rename an
    archive the other run already published. A sqlite3 shim plants a competing
    archive at this run's own candidate name right after VACUUM INTO finishes,
    simulating the other run winning the race to publish first; both archives
    must survive, this run's under the next free name.

    `date` is pinned (re-review nit 6) so the stamp reset-data.sh computes and
    the one the shim uses to name the competitor are the same value by
    construction, not by both happening to read the clock in the same real
    second -- and so the "-1" name below is the one actually exercised, not
    a guess about what the EEXIST retry would have produced."""
    db = make_db(data)
    real = shutil.which("sqlite3")
    assert real is not None
    stamp = "20260101T000000Z"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _date_shim(shim_dir, stamp)
    shim = shim_dir / "sqlite3"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  VACUUM*)\n"
        "    target=$(printf '%s' \"$2\" | sed -n \"s/^VACUUM INTO '\\\\(.*\\\\)'\\$/\\\\1/p\")\n"
        f'    "{real}" "$@"\n'
        "    rc=$?\n"
        '    archives=$(dirname "$(dirname "$target")")\n'
        f'    competitor="$archives/netkeep-{stamp}.sqlite3"\n'
        '    [ -e "$competitor" ] || printf \'a concurrent run published first\' > "$competitor"\n'
        "    exit $rc\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    path = f"{shim_dir}:{SYSTEM_PATH}"
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 0, result.stderr

    archives = data / "archives"
    competitor = archives / f"netkeep-{stamp}.sqlite3"
    ours = archives / f"netkeep-{stamp}-1.sqlite3"
    assert sorted(archives.iterdir()) == sorted([competitor, ours])
    assert competitor.read_bytes() == b"a concurrent run published first"
    with closing(sqlite3.connect(ours)) as check:
        assert check.execute("select x from marker").fetchone() == (42,)
    assert not db.exists()


@needs_sqlite3_cli
def test_reset_refuses_when_the_candidate_name_is_a_directory_symlink(
    data: Path, tmp_path: Path
) -> None:
    """Re-review should-fix 4: some `ln` implementations link *inside* a
    directory (or a symlink to one) found at the destination name instead of
    failing -- the candidate path itself is then still whatever it was
    before, not the archive it looks like. The live database must survive
    rather than being deleted on the strength of a publish that never
    actually happened where expected."""
    db = make_db(data)
    archives = data / "archives"
    archives.mkdir()
    stamp = "20260101T000000Z"
    candidate = archives / f"netkeep-{stamp}.sqlite3"
    trap_dir = tmp_path / "trap-dir"
    trap_dir.mkdir()
    candidate.symlink_to(trap_dir)

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _date_shim(shim_dir, stamp)
    path = f"{shim_dir}:{SYSTEM_PATH}"

    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert db.exists(), "the live database must survive"
    assert marker(db) == 42
    # ln's own directory-redirect placed the real copy inside trap_dir, under
    # the work file's basename ("db"); candidate itself is untouched.
    assert candidate.is_symlink()
    assert not (archives / f"netkeep-{stamp}-1.sqlite3").exists()


@needs_sqlite3_cli
def test_reset_treats_a_dangling_symlink_at_the_candidate_name_as_taken(
    data: Path, tmp_path: Path
) -> None:
    """Re-review round 3 nit 3: -e alone misses a dangling symlink (its
    target does not exist, so -e reads false even though the name itself is
    taken). A dangling symlink at the candidate name must be skipped like
    any other taken one -- retried under -1 -- not mistaken for a free name
    (which would make `ln` fail outright, read as a genuine publish
    failure)."""
    db = make_db(data)
    archives = data / "archives"
    archives.mkdir()
    stamp = "20260101T000000Z"
    candidate = archives / f"netkeep-{stamp}.sqlite3"
    candidate.symlink_to(archives / "does-not-exist")
    assert not candidate.exists()  # -e is false: the target is missing
    assert candidate.is_symlink()  # but -L is true: the name is still taken

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _date_shim(shim_dir, stamp)
    path = f"{shim_dir}:{SYSTEM_PATH}"

    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 0, result.stderr
    assert not db.exists()
    published = archives / f"netkeep-{stamp}-1.sqlite3"
    assert published.exists()
    with closing(sqlite3.connect(published)) as check:
        assert check.execute("select x from marker").fetchone() == (42,)
    # The dangling symlink itself is untouched -- only skipped past.
    assert candidate.is_symlink() and not candidate.exists()


@needs_sqlite3_cli
def test_reset_dies_clearly_when_quarantine_mv_itself_fails(data: Path, tmp_path: Path) -> None:
    """Re-review round 3 nit 3: quarantine_partial's own mv failure must not
    abort silently under set -e, nor (since it is called directly, not as
    `x=$(quarantine_partial ...)`) report from inside a vanished subshell
    while the original sat in $tmp, about to be removed by the EXIT trap --
    it must die with a message naming where the data actually, accurately,
    still is."""
    db = make_db(data)
    real_sqlite3 = shutil.which("sqlite3")
    assert real_sqlite3 is not None
    real_mv = shutil.which("mv")
    assert real_mv is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()

    # VACUUM "succeeds" but writes garbage, so verified() fails and
    # archive_db() reaches quarantine_partial().
    sqlite_shim = shim_dir / "sqlite3"
    sqlite_shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  VACUUM*)\n"
        "    target=$(printf '%s' \"$2\" | sed -n \"s/^VACUUM INTO '\\\\(.*\\\\)'\\$/\\\\1/p\")\n"
        "    printf 'not a real database' > \"$target\"\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real_sqlite3}" "$@"\n'
    )
    sqlite_shim.chmod(0o755)

    mv_shim = shim_dir / "mv"
    mv_shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *.failed) echo 'mv: simulated failure' >&2; exit 1 ;;\n"
        "esac\n"
        f'exec "{real_mv}" "$@"\n'
    )
    mv_shim.chmod(0o755)

    path = f"{shim_dir}:{SYSTEM_PATH}"
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "could not move the partial archive" in result.stderr
    assert marker(db) == 42
    archives = data / "archives"
    # Nothing survives anywhere: not published, not quarantined (mv itself
    # failed), and the temp directory is gone too -- the EXIT trap's job.
    assert not list(archives.glob("*.sqlite3"))
    assert not list(archives.glob("*.sqlite3.failed"))
    assert not list(archives.glob(".partial.*"))


@needs_sqlite3_cli
def test_reset_cleans_up_its_temp_dir_when_interrupted(data: Path, tmp_path: Path) -> None:
    """Re-review should-fix 2: an interrupted reset must not leave a hidden
    full copy of the database sitting in a .partial.* directory under
    archives/ -- an EXIT trap removes it regardless of how the script stops,
    and the INT/TERM/HUP traps turn an interrupt into a normal exit so that
    trap actually runs."""
    db = make_db(data)
    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    # The shim touches a ready file and then blocks, so the signal below
    # arrives only once the script is mid-archive with its traps installed --
    # not after a fixed delay that a loaded machine can overrun (#371).
    ready = tmp_path / "ready"
    shim.write_text(
        f'#!/bin/sh\ncase "$2" in VACUUM*) : > "{ready}"; sleep 60 ;; esac\nexec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    path = f"{shim_dir}:{SYSTEM_PATH}"
    assert SH is not None
    archives = data / "archives"
    proc = subprocess.Popen(
        [SH, str(RESET), "--data-dir", str(data), "--yes"],
        env={"PATH": path, "HOME": str(tmp_path / "no-home")},
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not ready.exists() and proc.poll() is None:
            time.sleep(0.05)
        assert ready.exists(), "archive_db() never reached the VACUUM step"
        assert any(archives.glob(".partial.*")), "archive_db() never created its temp directory"
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert not list(archives.glob(".partial.*")), "an interrupted reset left a partial copy behind"
    assert db.exists(), "the live database must survive an interrupted reset"
    assert marker(db) == 42


@needs_sqlite3_cli
def test_reset_folds_the_wal_when_vacuum_into_is_refused(data: Path, tmp_path: Path) -> None:
    """R9: VACUUM INTO can be refused (a read-only filesystem, a locked-down
    sqlite3 build); the raw-copy-and-fold fallback must still produce a
    complete, self-contained, verified archive with no leftover sidecars."""
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker (x integer)")
        conn.execute("INSERT INTO marker VALUES (1)")

    # A concurrent reader blocks the WAL from being fully checkpointed away on
    # close, so -wal still carries a real pending frame when reset-data.sh reads
    # it -- not just an artifact of the fallback path running at all.
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, time\n"
            f"conn = sqlite3.connect({str(db)!r})\n"
            "conn.execute('BEGIN')\n"
            "conn.execute('SELECT * FROM marker').fetchall()\n"
            "time.sleep(30)\n",
        ]
    )
    try:
        time.sleep(0.3)
        with closing(sqlite3.connect(db)) as writer, writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO marker VALUES (42)")
        assert Path(f"{db}-wal").stat().st_size > 0, "test setup needs a pending WAL"
    finally:
        reader.kill()
        reader.wait()

    real = shutil.which("sqlite3")
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    shim.write_text(f'#!/bin/sh\ncase "$2" in VACUUM*) exit 1 ;; esac\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    path = f"{shim_dir}:{SYSTEM_PATH}"

    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 0, result.stderr
    assert "copying the raw files instead" in result.stderr
    assert "verified" in result.stdout

    (archive,) = (data / "archives").iterdir()
    assert not Path(f"{archive}-wal").exists()
    assert not Path(f"{archive}-shm").exists()
    with closing(sqlite3.connect(archive)) as check:
        rows = check.execute("SELECT x FROM marker ORDER BY x").fetchall()
    assert rows == [(1,), (42,)]


@needs_sqlite3_cli
def test_reset_dies_when_the_wal_fold_pragma_fails(data: Path, tmp_path: Path) -> None:
    """Re-review round 3 should-fix 2: a failed WAL fold (the
    wal_checkpoint/journal_mode pragma, in the raw-copy fallback) is now
    fatal rather than a swallowed `|| true` warning -- quietly publishing an
    archive missing whatever was still in the WAL, with no sign anything was
    wrong, is worse than refusing. This holds the same on macOS and on
    Linux: unlike the old pin (nit 5, which read the copied -wal's presence
    at the moment SQLite's `-readonly` tried to open it -- a question CI's
    Linux build and the maintainer's macOS build answered differently), the
    fatal check here is the pragma's own exit status plus a plain `-s` file
    check, neither of which depends on SQLite's WAL-without-sidecars
    behavior at all.

    The marker records whether the copied -wal genuinely had content at the
    moment the (shimmed, failing) fold was attempted, proving the test's own
    setup is real -- not a vacuous pass because there was nothing to fold in
    the first place."""
    db = data / "netkeeper.sqlite3"
    db.unlink(missing_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker (x integer)")
        conn.execute("INSERT INTO marker VALUES (1)")
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, time\n"
            f"conn = sqlite3.connect({str(db)!r})\n"
            "conn.execute('BEGIN')\n"
            "conn.execute('SELECT * FROM marker').fetchall()\n"
            "time.sleep(30)\n",
        ]
    )
    try:
        time.sleep(0.3)
        with closing(sqlite3.connect(db)) as writer, writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO marker VALUES (42)")
        wal_size_before = Path(f"{db}-wal").stat().st_size
        assert wal_size_before > 0, "test setup needs a pending WAL"
    finally:
        # Released before reset-data.sh runs: held open any longer and
        # refuse_if_in_use() would refuse for an unrelated reason.
        reader.kill()
        reader.wait()

    real = shutil.which("sqlite3")
    assert real is not None
    marker_file = tmp_path / "wal-at-fold-attempt"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "sqlite3"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  VACUUM*) exit 1 ;;\n"
        "  *wal_checkpoint*)\n"
        '    if [ -s "$1-wal" ]; then\n'
        f"      printf present > {str(marker_file)!r}\n"
        "    else\n"
        f"      printf absent > {str(marker_file)!r}\n"
        "    fi\n"
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    path = f"{shim_dir}:{SYSTEM_PATH}"

    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": path})
    assert result.returncode == 1
    assert "could not fold the WAL" in result.stderr
    assert marker_file.read_text() == "present"
    # The live database (and its own -wal, untouched) must survive -- this
    # run never got anywhere near remove_db().
    assert db.exists()
    assert Path(f"{db}-wal").stat().st_size == wal_size_before
    assert not list((data / "archives").glob("*.sqlite3"))


@needs_sqlite3_cli
def test_reset_removes_a_leftover_wal_sidecar(data: Path) -> None:
    """R14: remove_db() must delete a leftover ``-wal``/``-shm``, not only the
    main file."""
    db = make_db(data)
    wal = Path(f"{db}-wal")
    shm = Path(f"{db}-shm")
    wal.write_bytes(b"stale wal frames")
    shm.write_bytes(b"stale shm")
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={})
    assert result.returncode == 0, result.stderr
    assert not db.exists()
    assert not wal.exists()
    assert not shm.exists()


def test_list_orders_a_same_second_collision_newest_first(data: Path) -> None:
    """#183 review bug 6: ``-1`` was written after the bare name (the same
    collision ``archive_path()`` resolves within one second), so it must list
    first, not second."""
    archives = data / "archives"
    archives.mkdir()
    (archives / "netkeep-20260101T000000Z.sqlite3").write_bytes(b"first")
    (archives / "netkeep-20260101T000000Z-1.sqlite3").write_bytes(b"second, same second")
    result = run_sh(RESET, "--data-dir", str(data), "--list", env={})
    assert result.returncode == 0, result.stderr
    names = [line.split("  ")[0] for line in result.stdout.splitlines()]
    assert names == [
        "netkeep-20260101T000000Z-1.sqlite3",
        "netkeep-20260101T000000Z.sqlite3",
    ]


@needs_sqlite3_cli
def test_reset_refuses_without_sqlite3(data: Path, tmp_path: Path) -> None:
    """R16: sqlite3's own tool-requirement check, exercised with lsof present --
    distinct from the missing-lsof test above."""
    make_db(data)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "lsof",
        "sed",
        "awk",
        "date",
        "mktemp",
        "wc",
        "tr",
        "sort",
        "basename",
        "dirname",
        "cp",
        "rm",
        "mkdir",
        "ps",
        "grep",
        "cat",
        "cut",
        "sh",
    ):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    result = run_sh(RESET, "--data-dir", str(data), "--yes", env={"PATH": str(bin_dir)})
    assert result.returncode == 1
    assert "sqlite3 is needed" in result.stderr
    assert (data / "netkeeper.sqlite3").exists()
