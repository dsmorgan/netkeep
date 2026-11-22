"""scripts/update-checkout.sh, which ``make update`` runs (#839), run through sh.

Each test builds a scratch ``origin`` repository and a clone of it under ``tmp_path``,
with the script committed in it. Real ``git`` runs against those, configured by a
scratch file only. ``uv``, ``pnpm`` and the clone's ``.venv/bin/netkeep`` are fakes that
log each call (with the commit the clone is at) and fail when a test says so: nothing
syncs, builds, reaches a login item or a ``serve``. ``PATH`` holds the fakes, ``git``
and the system folders, so no installed ``uv`` or ``pnpm`` can stand in for a fake.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "update-checkout.sh"
SH = shutil.which("sh")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    SH is None or GIT is None or sys.platform == "win32", reason="needs a POSIX sh and git"
)

#: A fake that logs ``<name> <arguments> @<commit> in <folder>`` and exits with
#: ``$FAIL_<NAME>_<WORD>``, else 0: ``FAIL_UV_SYNC``, ``FAIL_PNPM_BUILD``,
#: ``FAIL_NETKEEP_CHECKOUT_STOP`` (netkeep's word is its flag after ``update``).
FAKE = """#!/bin/sh
name=$(basename "$0")
head=$(git rev-parse --short HEAD 2>/dev/null || echo none)
printf '%s %s @%s in %s\\n' "$name" "$*" "$head" "$(basename "$(pwd)")" >>"$CALLS"
case $name in netkeep) word=${2#--} ;; *) word=$1 ;; esac
key=$(printf '%s_%s' "$name" "$word" | tr 'a-z-' 'A-Z_')
eval "status=\\${FAIL_$key:-0}"
exit "$status"
"""


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
    done = subprocess.run(
        [GIT or "git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@dataclass
class Repo:
    """A scratch origin, a seed clone that pushes to it, and the checkout under test."""

    tmp: Path
    env: dict[str, str]
    seed: Path
    checkout: Path
    calls: Path

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return _git(cwd or self.checkout, *args, env=self.env)

    def push_commit(self, name: str = "CHANGES", text: str = "a change\n") -> str:
        """A new commit on origin's main; its full hash."""
        (self.seed / name).write_text(text)
        self.git("add", name, cwd=self.seed)
        self.git("commit", "-q", "-m", f"add {name}", cwd=self.seed)
        self.git("push", "-q", "origin", "main", cwd=self.seed)
        return self.git("rev-parse", "HEAD", cwd=self.seed)

    @property
    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def short(self, commit: str) -> str:
        return self.git("rev-parse", "--short", commit)

    def run(self, *args: str, **fail: int) -> subprocess.CompletedProcess[str]:
        """The script as ``make update`` runs it, from the checkout; ``fail`` sets the
        exit status of a fake, e.g. ``UV_SYNC=1``."""
        env = self.env | {f"FAIL_{key}": str(value) for key, value in fail.items()}
        return subprocess.run(
            [SH or "sh", "scripts/update-checkout.sh", *args],
            cwd=self.checkout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    @property
    def logged(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    fakes = tmp_path / "bin"
    fakes.mkdir()
    for name in ("uv", "pnpm"):
        (fakes / name).write_text(FAKE)
        (fakes / name).chmod(0o755)
    (fakes / "git").symlink_to(GIT or "git")
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = Test Person\n\temail = test@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"
    )
    calls = tmp_path / "calls.log"
    env = {
        "PATH": f"{fakes}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_TERMINAL_PROMPT": "0",
        "CALLS": str(calls),
        "LC_ALL": "C",
    }
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin), env=env)
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed), env=env)
    (seed / "scripts").mkdir()
    shutil.copy2(SCRIPT, seed / "scripts" / "update-checkout.sh")
    (seed / "frontend").mkdir()
    (seed / "frontend" / "package.json").write_text("{}\n")
    (seed / ".gitignore").write_text(".venv/\nfrontend/dist/\n")
    _git(seed, "add", ".", env=env)
    _git(seed, "commit", "-q", "-m", "first", env=env)
    _git(seed, "push", "-q", "origin", "main", env=env)
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout), env=env)
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "netkeep").write_text(FAKE)
    (venv_bin / "netkeep").chmod(0o755)
    return Repo(tmp_path, env, seed, checkout, calls)


def _steps(repo: Repo, old: str, new: str) -> list[str]:
    """Every call a whole update makes, in order, each with the commit it ran on."""
    o, n = repo.short(old), repo.short(new)
    return [
        f"netkeep update --checkout-stop @{o} in checkout",
        f"uv sync --all-extras --frozen @{n} in checkout",
        f"pnpm install --frozen-lockfile @{n} in frontend",
        f"pnpm build @{n} in frontend",
        f"netkeep update --checkout-start @{n} in checkout",
    ]


# -- the whole update ------------------------------------------------------------------


def test_an_update_stops_pulls_syncs_builds_and_starts_in_order(repo: Repo) -> None:
    old = repo.head
    new = repo.push_commit()
    done = repo.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert repo.head == new
    # The stop runs on the old commit, before the pull; the rest on the new one.
    assert repo.logged == _steps(repo, old, new)
    assert f"main fast-forwards from {repo.short(old)} to {repo.short(new)}" in done.stdout
    assert f"Done: the checkout is at {repo.short(new)}." in done.stdout


def test_an_up_to_date_checkout_still_syncs_builds_and_starts(repo: Repo) -> None:
    """A `git pull` and `make install` by hand, without `make build-ui`, leave the UI
    old with nothing left to pull: make update still rebuilds it."""
    head = repo.head
    done = repo.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert repo.logged == _steps(repo, head, head)
    assert "nothing to pull" in done.stdout


def test_an_untracked_file_doesnt_block_the_update(repo: Repo) -> None:
    (repo.checkout / "notes.txt").write_text("mine\n")
    new = repo.push_commit()
    done = repo.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert repo.head == new and (repo.checkout / "notes.txt").exists()


# -- checks that change nothing ----------------------------------------------------------


def _changed_nothing(repo: Repo, done: subprocess.CompletedProcess[str], head: str) -> None:
    assert done.returncode != 0, done.stdout
    assert repo.head == head
    assert repo.logged == [], "a check that failed still ran a step"


def test_uncommitted_changes_are_refused(repo: Repo) -> None:
    head = repo.head
    repo.push_commit()
    (repo.checkout / ".gitignore").write_text("changed\n")
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert "the checkout has uncommitted changes" in done.stderr


def test_a_staged_change_is_refused(repo: Repo) -> None:
    head = repo.head
    (repo.checkout / "staged.txt").write_text("staged\n")
    repo.git("add", "staged.txt")
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert "uncommitted changes" in done.stderr


def test_another_branch_is_refused(repo: Repo) -> None:
    repo.git("switch", "-q", "-c", "feature")
    head = repo.head
    repo.push_commit()
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert "this checkout is on feature, not main" in done.stderr


def test_a_detached_head_is_refused(repo: Repo) -> None:
    repo.git("switch", "-q", "--detach")
    head = repo.head
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert "HEAD isn't on a branch" in done.stderr


def test_a_main_that_cant_fast_forward_is_refused(repo: Repo) -> None:
    (repo.checkout / "local.txt").write_text("local\n")
    repo.git("add", "local.txt")
    repo.git("commit", "-q", "-m", "local")
    head = repo.head
    repo.push_commit()
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert "can't fast-forward. Nothing was changed." in done.stderr


def test_a_failed_fetch_changes_nothing(repo: Repo) -> None:
    head = repo.head
    repo.git("remote", "set-url", "origin", str(repo.tmp / "missing.git"))
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert "git fetch origin main failed. Nothing was changed." in done.stderr


@pytest.mark.parametrize("tool", ["uv", "pnpm"])
def test_a_missing_tool_is_refused(repo: Repo, tool: str) -> None:
    (repo.tmp / "bin" / tool).unlink()
    head = repo.head
    repo.push_commit()
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert f"{tool} isn't on PATH" in done.stderr


def test_a_checkout_with_no_venv_is_told_to_set_up(repo: Repo) -> None:
    shutil.rmtree(repo.checkout / ".venv")
    head = repo.head
    done = repo.run()
    _changed_nothing(repo, done, head)
    assert "Set the checkout up first with: make setup" in done.stderr


def test_an_argument_is_refused(repo: Repo) -> None:
    done = repo.run("--yes")
    assert done.returncode == 2 and repo.logged == []
    assert "Usage:" in done.stderr


# -- failures along the way --------------------------------------------------------------


def test_a_failed_stop_changes_nothing(repo: Repo) -> None:
    """The stop step refuses a serve you run yourself, or its backup failed."""
    head = repo.head
    repo.push_commit()
    done = repo.run(NETKEEP_CHECKOUT_STOP=1)
    assert done.returncode == 1
    assert repo.head == head
    assert repo.logged == [f"netkeep update --checkout-stop @{repo.short(head)} in checkout"]
    assert "stopped before changing anything" in done.stdout


def test_a_failed_merge_starts_serve_again_on_the_old_code(repo: Repo) -> None:
    """An untracked file the pull would overwrite: git refuses, and nothing changed."""
    head = repo.head
    repo.push_commit("notes.txt", "theirs\n")
    (repo.checkout / "notes.txt").write_text("mine\n")
    done = repo.run()
    assert done.returncode == 1
    assert repo.head == head
    assert (repo.checkout / "notes.txt").read_text() == "mine\n"
    short = repo.short(head)
    assert repo.logged == [
        f"netkeep update --checkout-stop @{short} in checkout",
        f"netkeep update --checkout-start @{short} in checkout",
    ]
    assert "the checkout didn't change" in done.stdout


@pytest.mark.parametrize(
    ("fail", "why", "ran"),
    [
        ({"UV_SYNC": 1}, "uv sync --all-extras --frozen failed", 2),
        ({"PNPM_INSTALL": 1}, "the UI build failed", 3),
        ({"PNPM_BUILD": 1}, "the UI build failed", 4),
    ],
)
def test_a_failure_after_the_pull_says_where_things_stand(
    repo: Repo, fail: dict[str, int], why: str, ran: int
) -> None:
    old = repo.head
    new = repo.push_commit()
    done = repo.run(**fail)
    assert done.returncode == 1
    assert repo.head == new
    # It stops there: serve isn't started on code whose .venv or UI is half done.
    assert repo.logged == _steps(repo, old, new)[:ran]
    out = done.stdout
    assert f"make update stopped: {why}." in out
    assert f"The checkout is at {repo.short(new)}; it was at {repo.short(old)}." in out
    assert "the database is\n    as it was before the update" in out
    assert "run make update again" in out
    assert f"git reset --keep {old}" in out


def test_a_failed_start_says_how_to_go_back(repo: Repo) -> None:
    old = repo.head
    new = repo.push_commit()
    done = repo.run(NETKEEP_CHECKOUT_START=1)
    assert done.returncode == 1
    assert repo.logged == _steps(repo, old, new)
    out = done.stdout
    assert "netkeep serve didn't start on the new code" in out
    assert "may have migrated the database" in out
    go_back = [
        ".venv/bin/netkeep login-item off",
        f"git reset --keep {old}",
        "uv sync --all-extras --frozen",
        "make build-ui",
        ".venv/bin/netkeep restore <the backup step 2 named>",
        ".venv/bin/netkeep login-item on",
    ]
    lines = [line.strip() for line in out.splitlines()]
    start = lines.index(go_back[0])
    assert lines[start : start + len(go_back)] == go_back


def test_a_rerun_after_a_failure_finishes_the_update(repo: Repo) -> None:
    old = repo.head
    new = repo.push_commit()
    assert repo.run(UV_SYNC=1).returncode == 1
    repo.calls.unlink()
    done = repo.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert repo.logged == _steps(repo, new, new)
    assert old != new


# -- the Makefile ------------------------------------------------------------------------


def test_the_makefile_runs_the_script_and_keeps_install_and_check_as_they_were() -> None:
    """``make check`` (CI's Python side) depends on ``install`` alone, so it never needs
    pnpm or builds the UI; ``make setup`` is the first-time path."""
    rules = {}
    for line in (ROOT / "Makefile").read_text().splitlines():
        target, colon, rest = line.partition(":")
        if colon and target and not line.startswith(("\t", " ", "#")) and "=" not in target:
            rules[target] = rest.split("##")[0].split()
    text = (ROOT / "Makefile").read_text()
    assert "update:" in text and "\tscripts/update-checkout.sh\n" in text
    assert rules["setup"] == ["install", "build-ui"]
    assert rules["install"] == []
    assert rules["check"] == ["install", "lint", "typecheck", "test"]


def test_the_script_is_posix_sh_and_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/bin/sh\n")
    # Parsed whole before it runs, so the pull replacing it can't change what runs.
    assert SCRIPT.read_text().rstrip().endswith('main "$@"')
