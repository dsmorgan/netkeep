"""The one-line installer, scripts/install.sh (#686, ADR 0011 rule 5).

Users pipe this script to a shell, so its safety properties are pinned here. Each test
runs a stamped copy with a scratch HOME, PATH set to the sandbox's own bin plus
`/usr/bin:/bin`, every install location pointed into the sandbox, and fake `curl`, `uv`,
`netkeep`, `uname` and `id`. The fakes log every call, serve canned files, and refuse to
write outside the sandbox, so nothing is downloaded or installed for real, and a broken
script can't make a fake write elsewhere. The real `shasum`, `awk` and `mktemp` still
run. An autouse check asserts that no command of a fake's name changed on the real PATH.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install.sh"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

VERSION = "1.2.3"
RELEASE_URL = f"https://github.com/dsmorgan/netkeep/releases/download/v{VERSION}"
UV_INSTALLER_URL = "https://github.com/astral-sh/uv/releases/download/0.12.23/uv-installer.sh"
CONSTRAINTS = b"cryptography==50.0.1\nfastapi==0.200.0\n"
PLACEHOLDER_LINE = "    netkeep_version='@NETKEEP_VERSION@'"
_UV_PIN = re.compile(r"^    uv_installer_sha256='([0-9a-f]{64})'$", re.M)
_WORK = re.compile(r"/\.netkeep-install\.[A-Za-z0-9]+/")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")


def _stamp(text: str, version: str = VERSION) -> str:
    # The same substitution as release.yml's "Stamp the version into the installer".
    assert text.count(PLACEHOLDER_LINE + "\n") == 1
    return text.replace(PLACEHOLDER_LINE + "\n", f"    netkeep_version='{version}'\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The fakes. Each logs its argv, one line per call, to $FAKE_LOG, and refuses to write
# anywhere outside $FAKE_ROOT (the sandbox under tmp_path): a mutated or broken script
# that hands one an outside path gets exit 97 and a REFUSED line, never a real write.
_GUARD = r"""#!/bin/sh
case ${FAKE_ROOT:-} in /?*) ;; *) echo "fake: FAKE_ROOT unset" >&2; exit 97 ;; esac
case ${FAKE_LOG:-} in "$FAKE_ROOT"/*) ;; *) echo "fake: bad FAKE_LOG" >&2; exit 97 ;; esac
log() { printf '%s\n' "$*" >> "$FAKE_LOG"; }
confined() {
    case $1 in
        */../* | */..) ;;
        "$FAKE_ROOT"/*) return 0 ;;
    esac
    log "REFUSED write outside the sandbox: $1"
    echo "fake: refusing to write outside the sandbox: $1" >&2
    exit 97
}
"""

_FAKE_CURL = (
    _GUARD
    + r"""
log "curl $*"
out=''
url=''
while [ $# -gt 0 ]; do
    case $1 in
        -o) out=$2; shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
confined "$out"
name=$(printf '%s' "$url" | tr -c 'A-Za-z0-9._\n-' '_')
[ -f "$FAKE_SERVE/$name" ] || exit 22
cp "$FAKE_SERVE/$name" "$out"
"""
)

_FAKE_UV = (
    _GUARD
    + r"""
log "uv $*"
case "$1 $2" in
    "tool dir")
        printf '%s\n' "$UV_TOOL_BIN_DIR"
        ;;
    "tool install")
        if [ -n "${FAKE_UV_SLEEP:-}" ]; then
            sleep "$FAKE_UV_SLEEP"
            exit 1
        fi
        [ -z "${FAKE_UV_INSTALL_FAILS:-}" ] || exit 2
        [ -z "${FAKE_UV_NO_BIN:-}" ] || exit 0
        confined "$UV_TOOL_BIN_DIR/netkeep"
        mkdir -p "$UV_TOOL_BIN_DIR"
        cp "$FAKE_TEMPLATES/netkeep" "$UV_TOOL_BIN_DIR/netkeep"
        ;;
esac
"""
)

_FAKE_NETKEEP = (
    _GUARD
    + r"""
log "netkeep $*"
if [ "$*" = "setup --help" ] && [ -n "${FAKE_NO_SETUP:-}" ]; then
    echo "No such command 'setup'." >&2
    exit 2
fi
if [ "$*" = "setup" ] && [ -n "${FAKE_SETUP_FAILS:-}" ]; then
    echo "setup: something went wrong" >&2
    exit 1
fi
"""
)

# The variables uv 0.12.23's installer reads that redirect its download, send a token,
# move its install folder, or name a file outside HOME to append to. The script clears
# them all for the installer.
_INSTALLER_REDIRECTS = (
    "UV_DOWNLOAD_URL",
    "INSTALLER_DOWNLOAD_URL",
    "UV_INSTALLER_GHE_BASE_URL",
    "UV_INSTALLER_GITHUB_BASE_URL",
    "CARGO_DIST_FORCE_INSTALL_DIR",
    "UV_UNMANAGED_INSTALL",
    "UV_GITHUB_TOKEN",
    "GITHUB_PATH",
)

# uv's installer, as served. It logs any redirect variable it can see, then applies the
# real installer's PATH rule: it edits the shell profile only when its install folder
# isn't on PATH already. Then it puts the fake uv where it was told to.
_FAKE_UV_INSTALLER = (
    _GUARD
    + r"""
log "uv-installer UV_INSTALL_DIR=$UV_INSTALL_DIR"
for name in """
    + " ".join(_INSTALLER_REDIRECTS)
    + r"""; do
    eval "isset=\${$name+set}"
    [ -z "$isset" ] || log "uv-installer saw $name"
done
case ":$PATH:" in
    *":$UV_INSTALL_DIR:"*) log "uv-installer profile skipped: $UV_INSTALL_DIR is on PATH" ;;
    *) log "uv-installer profile updated" ;;
esac
[ -z "${FAKE_INSTALLER_NOOP:-}" ] || exit 0
confined "$UV_INSTALL_DIR/uv"
mkdir -p "$UV_INSTALL_DIR"
cp "$FAKE_TEMPLATES/uv" "$UV_INSTALL_DIR/uv"
"""
).encode()

_FAKE_UNAME = '#!/bin/sh\nprintf "%s\\n" "${FAKE_OS:-Darwin}"\n'
_FAKE_ID = '#!/bin/sh\nprintf "%s\\n" "${FAKE_UID:-501}"\n'
_FAKE_NAMES = ("curl", "uv", "uvx", "netkeep", "uname", "id")


def _served_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", url)


@dataclass
class Sandbox:
    root: Path
    home: Path
    outside: Path
    bin: Path
    templates: Path
    serve: Path
    log: Path
    script: Path

    def write_script(self, text: str) -> None:
        self.script.write_text(text, encoding="utf-8")

    def serve_file(self, url: str, data: bytes) -> None:
        (self.serve / _served_name(url)).write_bytes(data)

    def unserve(self, url: str) -> None:
        (self.serve / _served_name(url)).unlink()

    def uv_on_path(self) -> None:
        shutil.copy(self.templates / "uv", self.bin / "uv")

    @property
    def path(self) -> str:
        # The sandbox's own bin, then the system's: nothing from the real PATH.
        return f"{self.bin}:/usr/bin:/bin"

    def environ(self, env: dict[str, str], unset: tuple[str, ...] = ()) -> dict[str, str]:
        local = str(self.home / ".local")
        environ = {
            "HOME": str(self.home),
            "PATH": self.path,
            "TMPDIR": str(self.outside),
            # Every install location a uv or its installer reads, inside the sandbox.
            # UV_INSTALL_DIR is a decoy: the script must set its own.
            "UV_INSTALL_DIR": str(self.root / "decoy-install-dir"),
            "UV_TOOL_BIN_DIR": f"{local}/bin",
            "UV_TOOL_DIR": f"{local}/share/uv/tools",
            "UV_PYTHON_INSTALL_DIR": f"{local}/share/uv/python",
            "UV_CACHE_DIR": str(self.home / ".cache" / "uv"),
            "XDG_BIN_HOME": f"{local}/bin",
            "XDG_DATA_HOME": f"{local}/share",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "CARGO_HOME": str(self.home / ".cargo"),
            "FAKE_ROOT": str(self.root),
            "FAKE_LOG": str(self.log),
            "FAKE_SERVE": str(self.serve),
            "FAKE_TEMPLATES": str(self.templates),
            "LC_ALL": "C",
            **env,
        }
        for name in unset:
            environ.pop(name, None)
        return environ

    def run(
        self,
        *args: str,
        unset: tuple[str, ...] = (),
        extra: dict[str, str] | None = None,
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.script), *args],
            cwd=self.outside,
            env=self.environ({**(extra or {}), **env}, unset),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def pipe(self, shell: str, script: bytes) -> subprocess.CompletedProcess[bytes]:
        """Run the script the way users do: `curl ... | sh`."""
        return subprocess.run(
            [shell, "-s"],
            input=script,
            cwd=self.outside,
            env=self.environ({}),
            capture_output=True,
            timeout=20,
            check=False,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        text = self.log.read_text(encoding="utf-8")
        return _WORK.sub("/<work>/", text.replace(str(self.home), "<HOME>")).splitlines()


def _executable(path: Path, text: str | bytes) -> None:
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _files(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def _real_path_snapshot() -> dict[str, tuple[int, int, int] | None]:
    snapshot: dict[str, tuple[int, int, int] | None] = {}
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in _FAKE_NAMES:
            path = os.path.join(directory, name)
            try:
                st = os.stat(path)
            except OSError:
                snapshot[path] = None
            else:
                snapshot[path] = (st.st_ino, st.st_size, st.st_mtime_ns)
    return snapshot


@pytest.fixture(autouse=True)
def _real_path_unchanged() -> Iterator[None]:
    """No test here touches a command of a fake's name anywhere on the real PATH."""
    before = _real_path_snapshot()
    yield
    assert _real_path_snapshot() == before


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    """A stamped script, fakes on PATH, and a release with matching checksums.

    The sandbox is tmp_path/box, so tmp_path itself has room for a path outside it.
    uv starts missing, and the script's uv pin is restamped to the fake installer's hash
    (the real installer is never in a test)."""
    root = tmp_path / "box"
    sandbox = Sandbox(
        root=root,
        home=root / "home",
        outside=root / "outside",
        bin=root / "bin",
        templates=root / "templates",
        serve=root / "serve",
        log=root / "calls.log",
        script=root / "script" / "install.sh",
    )
    for d in (sandbox.home, sandbox.outside, sandbox.bin, sandbox.templates, sandbox.serve):
        d.mkdir(parents=True)
    sandbox.script.parent.mkdir()
    _executable(sandbox.bin / "curl", _FAKE_CURL)
    _executable(sandbox.bin / "uname", _FAKE_UNAME)
    _executable(sandbox.bin / "id", _FAKE_ID)
    _executable(sandbox.templates / "uv", _FAKE_UV)
    _executable(sandbox.templates / "netkeep", _FAKE_NETKEEP)

    text = _stamp(SCRIPT.read_text(encoding="utf-8"))
    text = _UV_PIN.sub(f"    uv_installer_sha256='{_sha256(_FAKE_UV_INSTALLER)}'", text, count=1)
    sandbox.write_script(text)
    sandbox.serve_file(UV_INSTALLER_URL, _FAKE_UV_INSTALLER)
    sandbox.serve_file(f"{RELEASE_URL}/constraints.txt", CONSTRAINTS)
    sums = (
        f"{'a' * 64}  netkeep-{VERSION}-py3-none-any.whl\n"
        # A decoy whose name contains the real one: only an exact name match is used.
        f"{'c' * 64}  evil-constraints.txt\n"
        f"{_sha256(CONSTRAINTS)}  constraints.txt\n"
        f"{'b' * 64}  install.sh\n"
    )
    sandbox.serve_file(f"{RELEASE_URL}/SHA256SUMS", sums.encode())
    return sandbox


_CURL = "curl --proto =https --tlsv1.2 -fsSL -o"
_INSTALL = (
    "uv tool install --python 3.12 --managed-python "
    f"-c <HOME>/<work>/constraints.txt netkeep=={VERSION}"
)
_RELEASE_CALLS = [
    f"{_CURL} <HOME>/<work>/constraints.txt {RELEASE_URL}/constraints.txt",
    f"{_CURL} <HOME>/<work>/SHA256SUMS {RELEASE_URL}/SHA256SUMS",
    _INSTALL,
    "uv tool dir --bin",
    "netkeep setup --help",
    "netkeep setup",
]
_UV_INSTALLER_CALLS = [
    f"{_CURL} <HOME>/<work>/uv-installer.sh {UV_INSTALLER_URL}",
    "uv-installer UV_INSTALL_DIR=<HOME>/.local/bin",
    "uv-installer profile updated",
]


# --- the commands ---------------------------------------------------------------------


def test_installs_uv_then_netkeep_with_the_checked_constraints_then_runs_setup(
    box: Sandbox,
) -> None:
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.calls() == [*_UV_INSTALLER_CALLS, *_RELEASE_CALLS]
    assert (box.home / ".local" / "bin" / "uv").exists()
    assert (box.home / ".local" / "bin" / "netkeep").exists()
    assert f"Done: netkeep {VERSION} is installed" in result.stdout


def test_an_installed_uv_is_used_and_not_reinstalled(box: Sandbox) -> None:
    box.uv_on_path()
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.calls() == _RELEASE_CALLS
    assert "uv is already installed" in result.stdout


def test_each_step_is_printed_before_it_runs(box: Sandbox) -> None:
    out = box.run().stdout
    steps = [
        "==> 1. Install uv 0.12.23",
        "==> 2. Download this release's constraints.txt",
        "==> 3. Install netkeep 1.2.3 as a uv tool",
        "==> 4. Run netkeep setup",
    ]
    positions = [out.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "netkeep==1.2.3" in out and "--managed-python" in out


def test_the_downloads_are_cleaned_up(box: Sandbox) -> None:
    box.run()
    assert not list(box.home.glob(".netkeep-install.*"))


def test_the_pins_are_literal() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert PLACEHOLDER_LINE + "\n" in text
    assert "    uv_version='0.12.23'\n" in text
    assert (
        "    uv_installer_sha256="
        "'b8e6c43099ee9f9a550984d3ad56948457c689e7a99c090b35377234ac241491'\n"
    ) in text
    assert "    netkeep_repo='dsmorgan/netkeep'\n" in text
    # uv's versioned installer from its GitHub release, never the moving astral.sh URL.
    assert (
        'uv_installer_url="https://github.com/astral-sh/uv/releases/download/'
        '$uv_version/uv-installer.sh"'
    ) in text
    assert "astral.sh/uv/install.sh" not in text


def test_never_uses_sudo() -> None:
    code = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if re.search(r"\bsudo\b", line) and "die " not in line]


# --- refusals -------------------------------------------------------------------------


def test_refuses_a_constraints_file_that_doesnt_match_sha256sums(box: Sandbox) -> None:
    box.uv_on_path()
    box.serve_file(f"{RELEASE_URL}/constraints.txt", CONSTRAINTS + b"evil==6.6.6\n")
    result = box.run()
    assert result.returncode != 0
    assert "constraints.txt has SHA-256" in result.stderr
    assert "Nothing was installed" in result.stderr
    assert not [c for c in box.calls() if c.startswith(("uv tool install", "netkeep"))]


def test_refuses_sha256sums_without_the_constraints_file(box: Sandbox) -> None:
    box.uv_on_path()
    box.serve_file(f"{RELEASE_URL}/SHA256SUMS", f"{'a' * 64}  install.sh\n".encode())
    result = box.run()
    assert result.returncode != 0
    assert "no single checksum for constraints.txt" in result.stderr
    assert not [c for c in box.calls() if c.startswith("uv tool install")]


def test_refuses_a_uv_installer_that_doesnt_match_its_pin(box: Sandbox) -> None:
    box.serve_file(UV_INSTALLER_URL, _FAKE_UV_INSTALLER + b"echo tampered\n")
    result = box.run()
    assert result.returncode != 0
    assert "not the pinned" in result.stderr
    calls = box.calls()
    assert calls == [f"{_CURL} <HOME>/<work>/uv-installer.sh {UV_INSTALLER_URL}"]
    assert not (box.home / ".local" / "bin" / "uv").exists()


def test_a_failed_download_stops_the_install(box: Sandbox) -> None:
    box.uv_on_path()
    box.unserve(f"{RELEASE_URL}/SHA256SUMS")
    result = box.run()
    assert result.returncode != 0
    assert f"could not download {RELEASE_URL}/SHA256SUMS" in result.stderr
    assert not [c for c in box.calls() if c.startswith("uv tool install")]


def test_a_failed_uv_tool_install_stops_before_setup(box: Sandbox) -> None:
    box.uv_on_path()
    result = box.run(FAKE_UV_INSTALL_FAILS="1")
    assert result.returncode != 0
    assert "uv tool install failed" in result.stderr
    assert not [c for c in box.calls() if c.startswith("netkeep")]


def test_an_unstamped_script_refuses_to_run(box: Sandbox) -> None:
    box.script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    result = box.run()
    assert result.returncode != 0
    assert "no release version stamped in" in result.stderr
    assert box.calls() == []


@pytest.mark.parametrize("os_name", ["Linux", "FreeBSD"])
def test_other_platforms_get_a_pointer_to_the_docs(box: Sandbox, os_name: str) -> None:
    result = box.run(FAKE_OS=os_name)
    assert result.returncode != 0
    assert "macOS only" in result.stderr and os_name in result.stderr
    assert "https://github.com/dsmorgan/netkeep#installing" in result.stderr
    assert box.calls() == []


def test_refuses_to_run_as_root(box: Sandbox) -> None:
    result = box.run(FAKE_UID="0")
    assert result.returncode != 0
    assert "sudo" in result.stderr
    assert box.calls() == []


def test_an_unknown_option_is_refused(box: Sandbox) -> None:
    result = box.run("--yes")
    assert result.returncode != 0
    assert "unknown option: --yes" in result.stderr
    assert box.calls() == []


# --- whole download only --------------------------------------------------------------


def test_the_body_is_one_function_called_on_the_last_line() -> None:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == '{ netkeep_install "$@"; }'
    code = [line for line in lines[:-1] if line.strip() and not line.startswith("#")]
    assert code[0] == "netkeep_install() {"
    assert code[-1] == "}"
    # Everything between is indented: the function's body, not top-level commands.
    assert all(line.startswith(" ") for line in code[1:-1])


def test_a_truncated_download_runs_nothing(box: Sandbox) -> None:
    full = box.script.read_bytes()
    # Every line boundary, a cut inside each line, and every byte of the last line.
    cuts = set()
    offset = 0
    for line in full.splitlines(keepends=True):
        cuts.update({offset, offset + len(line) // 2})
        offset += len(line)
    last_line_start = full.rstrip(b"\n").rfind(b"\n") + 1
    cuts.update(range(last_line_start, len(full.rstrip(b"\n"))))
    cuts.discard(len(full))
    for cut in sorted(cuts):
        box.script.write_bytes(full[:cut])
        result = box.run()
        assert box.calls() == [], f"cut at byte {cut} ran: {box.calls()}"
        assert "==>" not in result.stdout, f"cut at byte {cut} printed a step"
    assert _files(box.home) == []
    assert _files(box.outside) == []


# --- nothing outside HOME -------------------------------------------------------------


def test_writes_nothing_outside_home(box: Sandbox) -> None:
    before = {d: _files(box.root / d) for d in ("bin", "templates", "serve", "script")}
    result = box.run()
    assert result.returncode == 0, result.stderr
    # TMPDIR and the working directory are both `outside`: mktemp went under HOME.
    assert _files(box.outside) == []
    assert {d: _files(box.root / d) for d in before} == before
    assert sorted(p.name for p in box.root.iterdir()) == sorted(
        ["bin", "calls.log", "home", "outside", "script", "serve", "templates"]
    )
    # The only writes are the fakes' installs, under HOME.
    assert _files(box.home) == [".local", ".local/bin", ".local/bin/netkeep", ".local/bin/uv"]


# --- dry run --------------------------------------------------------------------------


def test_dry_run_prints_every_step_and_runs_none(box: Sandbox) -> None:
    result = box.run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert box.calls() == []
    assert _files(box.home) == []
    assert _files(box.outside) == []
    out = result.stdout
    for text in (
        "dry run",
        "==> 1. Install uv 0.12.23",
        f"download {UV_INSTALLER_URL}",
        f"download {RELEASE_URL}/constraints.txt",
        f"download {RELEASE_URL}/SHA256SUMS",
        "uv tool install --python 3.12 --managed-python -c",
        f"netkeep=={VERSION}",
        "==> 4. Run netkeep setup",
    ):
        assert text in out


def test_dry_run_with_uv_present_still_runs_nothing(box: Sandbox) -> None:
    box.uv_on_path()
    result = box.run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert box.calls() == []
    assert "uv is already installed" in result.stdout


# --- netkeep setup --------------------------------------------------------------------


def test_skips_setup_with_a_message_when_this_version_has_none(box: Sandbox) -> None:
    box.uv_on_path()
    result = box.run(FAKE_NO_SETUP="1")
    assert result.returncode == 0, result.stderr
    calls = box.calls()
    assert calls[-1] == "netkeep setup --help"
    assert "netkeep setup" not in calls
    assert "has no 'netkeep setup' command, so the installer skips it" in result.stdout
    assert "netkeep serve" in result.stdout


# --- PATH -----------------------------------------------------------------------------


def test_a_fresh_uv_install_sees_the_callers_path_and_gets_a_full_path_hint(
    box: Sandbox,
) -> None:
    # ~/.local/bin isn't on the caller's PATH: uv's installer must see that, so it edits
    # the shell profile, and the hint must name uv by its full path, since a new
    # terminal may not find a bare `uv` either.
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert "uv-installer profile updated" in box.calls()
    out = result.stdout.replace(str(box.home), "<HOME>")
    assert "<HOME>/.local/bin isn't on this terminal's PATH" in out
    assert "'<HOME>/.local/bin/uv' tool update-shell" in out
    assert 'export PATH="<HOME>/.local/bin:$PATH"' in out


def test_no_path_hint_when_the_folder_is_already_on_path(box: Sandbox) -> None:
    result = box.run(PATH=f"{box.home}/.local/bin:{box.path}")
    assert result.returncode == 0, result.stderr
    assert any(c.startswith("uv-installer profile skipped") for c in box.calls())
    assert "isn't on this terminal's PATH" not in result.stdout


def test_a_uv_in_local_bin_but_not_on_path_is_used(box: Sandbox) -> None:
    (box.home / ".local" / "bin").mkdir(parents=True)
    shutil.copy(box.templates / "uv", box.home / ".local" / "bin" / "uv")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.calls() == _RELEASE_CALLS
    assert f"uv is already installed: {box.home}/.local/bin/uv" in result.stdout


# --- uv's installer environment -------------------------------------------------------


@pytest.mark.parametrize("name", _INSTALLER_REDIRECTS)
def test_uv_installer_never_sees_a_redirect_variable(box: Sandbox, name: str) -> None:
    value = str(box.root / "redirected") if name == "GITHUB_PATH" else "https://evil.invalid"
    result = box.run(extra={name: value})
    assert result.returncode == 0, result.stderr
    assert box.calls() == [*_UV_INSTALLER_CALLS, *_RELEASE_CALLS]


def test_uv_installer_is_told_the_home_install_dir_not_the_callers(box: Sandbox) -> None:
    # The environment's UV_INSTALL_DIR is a decoy; the logged one must be HOME's.
    box.run()
    assert "uv-installer UV_INSTALL_DIR=<HOME>/.local/bin" in box.calls()
    assert not (box.root / "decoy-install-dir").exists()


def test_a_fake_refuses_an_install_dir_outside_the_sandbox(box: Sandbox, tmp_path: Path) -> None:
    # The shape of the mutation that once wrote a fake uv into a real PATH folder: the
    # script hands uv's installer a folder outside HOME. The fake must refuse it.
    escape = tmp_path / "escape"
    text = box.script.read_text(encoding="utf-8")
    assert text.count('UV_INSTALL_DIR="$local_bin" sh') == 1
    box.write_script(
        text.replace('UV_INSTALL_DIR="$local_bin" sh', f'UV_INSTALL_DIR="{escape}" sh')
    )
    result = box.run()
    assert result.returncode != 0
    assert f"REFUSED write outside the sandbox: {escape}/uv" in box.calls()
    assert not escape.exists()


def test_stops_when_uvs_installer_leaves_no_uv(box: Sandbox) -> None:
    result = box.run(FAKE_INSTALLER_NOOP="1")
    assert result.returncode != 0
    assert "there is no uv in" in result.stderr
    assert box.calls() == [*_UV_INSTALLER_CALLS]


# --- HOME -----------------------------------------------------------------------------


@pytest.mark.parametrize("home", ["", "/", "missing"])
def test_refuses_a_home_that_isnt_a_home_directory(box: Sandbox, home: str) -> None:
    if home == "/" and os.geteuid() == 0:
        pytest.skip("as root, a broken check could write to /")
    value = str(box.root / "no-such-home") if home == "missing" else home
    result = box.run(HOME=value)
    assert result.returncode != 0
    assert "HOME must be your home directory" in result.stderr
    assert box.calls() == []


def test_refuses_an_unset_home(box: Sandbox) -> None:
    result = box.run(unset=("HOME",))
    assert result.returncode != 0
    assert "HOME must be your home directory" in result.stderr
    assert box.calls() == []


# --- more refusals --------------------------------------------------------------------


def test_refuses_a_checksum_that_isnt_64_hex_digits(box: Sandbox) -> None:
    box.uv_on_path()
    short = _sha256(CONSTRAINTS)[:63]
    box.serve_file(f"{RELEASE_URL}/SHA256SUMS", f"{short}  constraints.txt\n".encode())
    result = box.run()
    assert result.returncode != 0
    assert "no valid checksum for constraints.txt" in result.stderr


def test_the_exact_name_is_used_not_a_decoy(box: Sandbox) -> None:
    # The fixture's SHA256SUMS lists evil-constraints.txt first; the install still
    # matches constraints.txt's own line.
    box.uv_on_path()
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert f"constraints.txt matches SHA256SUMS ({_sha256(CONSTRAINTS)})" in result.stdout


def test_stops_when_the_netkeep_command_is_missing_after_install(box: Sandbox) -> None:
    box.uv_on_path()
    result = box.run(FAKE_UV_NO_BIN="1")
    assert result.returncode != 0
    assert "netkeep was installed, but" in result.stderr
    assert not [c for c in box.calls() if c.startswith("netkeep")]


def test_a_failed_netkeep_setup_fails_the_install(box: Sandbox) -> None:
    box.uv_on_path()
    result = box.run(FAKE_SETUP_FAILS="1")
    assert result.returncode != 0
    assert "netkeep setup failed" in result.stderr
    assert box.calls()[-1] == "netkeep setup"


_DASH = shutil.which("dash", path="/usr/bin:/bin")


@pytest.mark.parametrize(
    "shell",
    [
        "/bin/sh",
        pytest.param(_DASH, marks=pytest.mark.skipif(_DASH is None, reason="no dash")),
    ],
)
def test_a_signal_still_removes_the_downloads(box: Sandbox, shell: str | None) -> None:
    # dash runs no EXIT trap when a signal kills it; the script's signal trap turns the
    # signal into an exit, so the download folder is removed either way.
    assert shell is not None
    box.uv_on_path()
    proc = subprocess.Popen(
        [shell, str(box.script)],
        cwd=box.outside,
        env=box.environ({"FAKE_UV_SLEEP": "1"}),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not any(c.startswith("uv tool install") for c in box.calls()):
            assert time.monotonic() < deadline, "the install never started"
            time.sleep(0.02)
        assert list(box.home.glob(".netkeep-install.*"))
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) != 0
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    assert not list(box.home.glob(".netkeep-install.*"))


def test_a_truncated_download_piped_to_a_shell_runs_nothing(box: Sandbox) -> None:
    full = box.script.read_bytes()
    cuts = set()
    offset = 0
    for line in full.splitlines(keepends=True):
        cuts.add(offset)
        offset += len(line)
    last_line_start = full.rstrip(b"\n").rfind(b"\n") + 1
    cuts.update(range(last_line_start, len(full.rstrip(b"\n"))))
    cuts.discard(len(full))
    shells = ["/bin/sh"] + ([_DASH] if _DASH else [])
    for shell in shells:
        for cut in sorted(cuts):
            result = box.pipe(shell, full[:cut])
            assert box.calls() == [], f"{shell}, cut at byte {cut}, ran: {box.calls()}"
            assert b"==>" not in result.stdout, f"{shell}, cut at byte {cut}, printed a step"
    assert _files(box.home) == []
    assert _files(box.outside) == []
    # And the whole script, piped, does run (as a dry run).
    whole = subprocess.run(
        ["/bin/sh", "-s", "--", "--dry-run"],
        input=full,
        cwd=box.outside,
        env=box.environ({}),
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert whole.returncode == 0 and b"==> 4. Run netkeep setup" in whole.stdout


# --- lint and release ------------------------------------------------------------------


def test_shellcheck_is_clean() -> None:
    shellcheck = Path(sys.executable).parent / "shellcheck"
    if not shellcheck.exists():
        found = shutil.which("shellcheck")
        assert found, "shellcheck missing: `make install` installs shellcheck-py"
        shellcheck = Path(found)
    result = subprocess.run(
        [str(shellcheck), "--shell=sh", "--severity=style", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_script_is_posix_sh_and_executable() -> None:
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert os.access(SCRIPT, os.X_OK)


def test_the_release_stamps_the_installer_and_attaches_it_with_its_checksum() -> None:
    jobs = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]
    build = "\n".join(str(step.get("run", "")) for step in jobs["build"]["steps"])
    stamp = (
        "sed \"s/^    netkeep_version='@NETKEEP_VERSION@'\\$/    netkeep_version='$VERSION'/\" "
        "scripts/install.sh > dist/install.sh"
    )
    assert stamp in build
    assert "grep -q '@NETKEEP_VERSION@' dist/install.sh" in build
    assert "constraints.txt install.sh > SHA256SUMS" in build
    assert build.index(stamp) < build.index("> SHA256SUMS")
    assets = [
        s for s in jobs["build"]["steps"] if s.get("with", {}).get("name") == "release-assets"
    ]
    assert "dist/install.sh" in assets[0]["with"]["path"]
    release = "\n".join(str(step.get("run", "")) for step in jobs["github-release"]["steps"])
    assert "constraints.txt install.sh SHA256SUMS" in release
    assert "releases/download/$TAG/install.sh | sh" in release
    smoke = "\n".join(str(step.get("run", "")) for step in jobs["smoke-install"]["steps"])
    assert "sh release/install.sh --dry-run" in smoke


def test_the_stamp_matches_the_tests_stamp(tmp_path: Path) -> None:
    # Run release.yml's sed on the script and compare it with _stamp().
    out = tmp_path / "install.sh"
    with out.open("w", encoding="utf-8") as fh:
        subprocess.run(
            [
                "sed",
                f"s/^    netkeep_version='@NETKEEP_VERSION@'$/    netkeep_version='{VERSION}'/",
                str(SCRIPT),
            ],
            stdout=fh,
            check=True,
            timeout=10,
        )
    assert out.read_text(encoding="utf-8") == _stamp(SCRIPT.read_text(encoding="utf-8"))
