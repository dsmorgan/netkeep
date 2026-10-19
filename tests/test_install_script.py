"""The one-line installer, scripts/install.sh (#686, ADR 0011 rule 5).

Users pipe this script to a shell, so its safety properties are pinned here. Each test
runs a stamped copy with `sh`, a scratch HOME, and fake `curl`, `uv`, `netkeep`,
`uname` and `id` first on PATH: the fakes log every call and serve canned files, so
nothing is downloaded or installed for real. The real `shasum`, `awk` and `mktemp`
still run.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
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


# The fakes. Each logs its argv, one line per call, to $FAKE_LOG.
_FAKE_CURL = r"""#!/bin/sh
printf 'curl %s\n' "$*" >> "$FAKE_LOG"
out=''
url=''
while [ $# -gt 0 ]; do
    case $1 in
        -o) out=$2; shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
name=$(printf '%s' "$url" | tr -c 'A-Za-z0-9._\n-' '_')
[ -f "$FAKE_SERVE/$name" ] || exit 22
cp "$FAKE_SERVE/$name" "$out"
"""

_FAKE_UV = r"""#!/bin/sh
printf 'uv %s\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
    "tool dir")
        printf '%s\n' "$HOME/.local/bin"
        ;;
    "tool install")
        [ -z "${FAKE_UV_INSTALL_FAILS:-}" ] || exit 2
        mkdir -p "$HOME/.local/bin"
        cp "$FAKE_TEMPLATES/netkeep" "$HOME/.local/bin/netkeep"
        ;;
esac
"""

_FAKE_NETKEEP = r"""#!/bin/sh
printf 'netkeep %s\n' "$*" >> "$FAKE_LOG"
if [ "$*" = "setup --help" ] && [ -n "${FAKE_NO_SETUP:-}" ]; then
    echo "No such command 'setup'." >&2
    exit 2
fi
"""

# uv's installer, as served: it puts the fake uv where it was told to.
_FAKE_UV_INSTALLER = rb"""#!/bin/sh
printf 'uv-installer UV_INSTALL_DIR=%s\n' "$UV_INSTALL_DIR" >> "$FAKE_LOG"
mkdir -p "$UV_INSTALL_DIR"
cp "$FAKE_TEMPLATES/uv" "$UV_INSTALL_DIR/uv"
"""

_FAKE_UNAME = '#!/bin/sh\nprintf "%s\\n" "${FAKE_OS:-Darwin}"\n'
_FAKE_ID = '#!/bin/sh\nprintf "%s\\n" "${FAKE_UID:-501}"\n'


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

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environ = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(self.outside),
            "FAKE_LOG": str(self.log),
            "FAKE_SERVE": str(self.serve),
            "FAKE_TEMPLATES": str(self.templates),
            "LC_ALL": "C",
            **env,
        }
        return subprocess.run(
            ["/bin/sh", str(self.script), *args],
            cwd=self.outside,
            env=environ,
            capture_output=True,
            text=True,
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


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    """A stamped script, fakes on PATH, and a release with matching checksums.

    uv starts missing, and the script's uv pin is restamped to the fake installer's hash
    (the real installer is never in a test)."""
    sandbox = Sandbox(
        root=tmp_path,
        home=tmp_path / "home",
        outside=tmp_path / "outside",
        bin=tmp_path / "bin",
        templates=tmp_path / "templates",
        serve=tmp_path / "serve",
        log=tmp_path / "calls.log",
        script=tmp_path / "script" / "install.sh",
    )
    for d in (sandbox.home, sandbox.outside, sandbox.bin, sandbox.templates, sandbox.serve):
        d.mkdir()
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


# --- the commands ---------------------------------------------------------------------


def test_installs_uv_then_netkeep_with_the_checked_constraints_then_runs_setup(
    box: Sandbox,
) -> None:
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.calls() == [
        f"{_CURL} <HOME>/<work>/uv-installer.sh {UV_INSTALLER_URL}",
        "uv-installer UV_INSTALL_DIR=<HOME>/.local/bin",
        *_RELEASE_CALLS,
    ]
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
