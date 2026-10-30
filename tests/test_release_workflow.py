"""The release workflow's safety properties (#683, ADR 0011 rules 1, 2 and 4).

release.yml publishes code under the project's name, so the properties that keep that
safe are pinned here: trusted publishing with no stored token, the protected `release`
environment, the OIDC permission on the publish job alone, actions pinned to commit
SHAs, the tag/version check, never replacing a release, and the constraints file with
the cryptography pair. The version check itself is scripts/check_release_version.py,
tested below on files written to tmp_path.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

import netkeep

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# A full commit SHA, then the version it is, as a comment: `owner/repo@<40 hex> # v1.2.3`.
_PINNED_USES = re.compile(r"^\s*-?\s*uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*\s*$")
_EXPORT = "uv export --frozen --no-dev --no-hashes --no-emit-project"


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads a bare `on` key as the boolean True.
    on = workflow.get("on", workflow.get(True))
    assert isinstance(on, dict)
    return on


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return steps


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _uses(job: dict[str, Any]) -> list[str]:
    return [str(step["uses"]) for step in _steps(job) if "uses" in step]


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    return _load(RELEASE)


@pytest.fixture(scope="module")
def jobs(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = release["jobs"]
    assert isinstance(found, dict)
    return found


# --- triggers and permissions ---------------------------------------------------------


def test_runs_on_a_version_tag_or_by_hand_only(release: dict[str, Any]) -> None:
    on = _triggers(release)
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"] == {"tags": ["v*"]}
    # A dry run takes no inputs: nothing a dispatcher types can steer it.
    assert on["workflow_dispatch"] is None


def test_dry_runs_and_tag_runs_queue_apart(release: dict[str, Any]) -> None:
    # Keyed on the event: a manual dry run can't cancel or displace a pending tag run,
    # and no run is cancelled halfway through publishing.
    assert release["concurrency"] == {
        "group": "release-${{ github.event_name }}",
        "cancel-in-progress": False,
    }


def test_no_expression_is_spliced_into_a_shell_step(jobs: dict[str, dict[str, Any]]) -> None:
    # Values reach `run:` through `env:` only, so nothing a tag name or another input
    # carries can become shell text (template injection).
    for name, job in jobs.items():
        for step in _steps(job):
            assert "${{" not in str(step.get("run", "")), (name, step.get("name"))


def test_nothing_has_write_access_by_default(release: dict[str, Any]) -> None:
    assert release["permissions"] == {}


def test_every_job_names_its_permissions(jobs: dict[str, dict[str, Any]]) -> None:
    for name, job in jobs.items():
        assert "permissions" in job, name


def test_only_the_publish_job_can_get_an_oidc_token(jobs: dict[str, dict[str, Any]]) -> None:
    with_token = [name for name, job in jobs.items() if "id-token" in job["permissions"]]
    assert with_token == ["publish"]
    # And it has nothing else: no contents, no packages, no write anywhere.
    assert jobs["publish"]["permissions"] == {"id-token": "write"}


def test_only_the_github_release_job_can_write_contents(
    jobs: dict[str, dict[str, Any]],
) -> None:
    writers = [name for name, job in jobs.items() if "write" in job["permissions"].values()]
    assert sorted(writers) == ["github-release", "publish"]
    assert jobs["github-release"]["permissions"] == {"contents": "write"}


# --- publishing -----------------------------------------------------------------------


def test_publishes_with_trusted_publishing_from_the_release_environment(
    jobs: dict[str, dict[str, Any]],
) -> None:
    publish = jobs["publish"]
    assert publish["environment"]["name"] == "release"
    (action,) = [u for u in _uses(publish) if u.startswith("pypa/gh-action-pypi-publish@")]
    (step,) = [s for s in _steps(publish) if s.get("uses") == action]
    # No password, no user, no token: the action exchanges the OIDC token itself. No
    # skip-existing either, so a file PyPI already has fails the run.
    assert step.get("with") is None


def test_no_stored_secret_is_used_anywhere() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "PYPI_TOKEN" not in text
    assert "password" not in text.lower()


def test_only_a_tag_push_publishes(jobs: dict[str, dict[str, Any]]) -> None:
    gate = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    assert jobs["publish"]["if"] == gate
    assert jobs["github-release"]["if"] == gate
    # Everything else runs on a dry run too.
    for name in ("build", "smoke-install"):
        assert "if" not in jobs[name], name


def test_publishing_waits_for_the_checks_and_both_macs(
    jobs: dict[str, dict[str, Any]],
) -> None:
    assert set(jobs["publish"]["needs"]) == {"build", "smoke-install"}
    assert jobs["smoke-install"]["needs"] == "build"
    # The GitHub release comes after PyPI, so its install command works when it appears.
    assert "publish" in jobs["github-release"]["needs"]


def test_a_release_is_never_replaced(jobs: dict[str, dict[str, Any]]) -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert "--clobber" not in text
    assert "skip-existing" not in text
    assert "gh release upload" not in text
    for name in ("build", "github-release"):
        runs = _runs(jobs[name])
        assert 'gh release view "$TAG"' in runs, name
        assert "already exists" in runs, name
    create = _runs(jobs["github-release"])
    assert "gh release create" in create
    assert "--verify-tag" in create
    # A draft until every file is attached, then published once.
    assert "--draft " in create
    assert "--draft=false" in create


def test_the_release_carries_the_four_files_and_checks_them(
    jobs: dict[str, dict[str, Any]],
) -> None:
    create = _runs(jobs["github-release"])
    for name in (
        '"netkeep-$VERSION-py3-none-any.whl"',
        '"netkeep-$VERSION.tar.gz"',
        "constraints.txt",
        "SHA256SUMS",
    ):
        assert name in create
    assert "sha256sum -c SHA256SUMS" in create
    assert "sha256sum -c -" in _runs(jobs["publish"])
    assert "shasum -a 256 -c SHA256SUMS" in _runs(jobs["smoke-install"])
    build = _runs(jobs["build"])
    assert "sha256sum " in build and "> SHA256SUMS" in build


# --- pins -----------------------------------------------------------------------------


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    lines = [ln for ln in RELEASE.read_text(encoding="utf-8").splitlines() if "uses:" in ln]
    assert lines
    unpinned = [ln.strip() for ln in lines if not _PINNED_USES.match(ln)]
    assert unpinned == []


def test_the_pin_pattern_rejects_tags_and_short_shas() -> None:
    assert _PINNED_USES.match(f"      - uses: actions/checkout@{'a' * 40} # v7.0.1")
    assert not _PINNED_USES.match("      - uses: actions/checkout@v7")
    assert not _PINNED_USES.match("      - uses: actions/checkout@v7.0.1")
    assert not _PINNED_USES.match(f"      - uses: actions/checkout@{'a' * 12} # v7.0.1")
    assert not _PINNED_USES.match(f"      - uses: actions/checkout@{'a' * 40}")


def test_checkouts_keep_no_credentials(jobs: dict[str, dict[str, Any]]) -> None:
    for name, job in jobs.items():
        for step in _steps(job):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name


def test_a_release_build_uses_no_cache(jobs: dict[str, dict[str, Any]]) -> None:
    for name, job in jobs.items():
        for step in _steps(job):
            uses = str(step.get("uses", ""))
            if uses.startswith("astral-sh/setup-uv@"):
                assert step["with"]["enable-cache"] is False, name
            if uses.startswith("actions/setup-node@"):
                assert "cache" not in step["with"], name
                assert step["with"]["package-manager-cache"] is False, name


# --- the version check ----------------------------------------------------------------


def test_the_build_checks_the_tag_before_building(jobs: dict[str, dict[str, Any]]) -> None:
    steps = _steps(jobs["build"])
    (check,) = [s for s in steps if s.get("id") == "version"]
    assert check["env"]["TAG"] == "${{ github.ref_name }}"
    assert 'scripts/check_release_version.py --tag "$TAG"' in check["run"]
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in check["run"]
    assert steps.index(check) < [s.get("run") for s in steps].index("uv build")
    # The wheel's own name is checked against the version the check printed.
    assert 'test -f "netkeep-$VERSION-py3-none-any.whl"' in _runs(jobs["build"])


def test_the_build_reuses_the_wheel_ships_the_ui_check(
    jobs: dict[str, dict[str, Any]],
) -> None:
    ci_wheel = _load(CI)["jobs"]["wheel"]
    release_runs = [s.get("run") for s in _steps(jobs["build"]) if "run" in s]
    for command in ("make build-ui", "uv build", "scripts/smoke-wheel.sh"):
        assert command in [s.get("run") for s in _steps(ci_wheel)]
        assert command in release_runs


@pytest.fixture(scope="module")
def checker() -> Iterator[ModuleType]:
    path = ROOT / "scripts" / "check_release_version.py"
    spec = importlib.util.spec_from_file_location("check_release_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


def _package(tmp_path: Path, project: str, module: str) -> tuple[Path, Path]:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nname = "netkeep"\nversion = "{project}"\n')
    init = tmp_path / "__init__.py"
    init.write_text(f'"""netkeep."""\n\n__version__ = "{module}"\n')
    return pyproject, init


def test_a_matching_tag_passes_and_prints_the_version(
    checker: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject, init = _package(tmp_path, "1.2.3", "1.2.3")
    args = ["--tag", "v1.2.3", "--pyproject", str(pyproject), "--init", str(init)]
    assert checker.main(args) == 0
    assert capsys.readouterr().out == "1.2.3\n"


@pytest.mark.parametrize(
    ("tag", "message"),
    [
        ("v1.2.4", "does not match the package version 1.2.3"),
        ("v1.2", "is not of the form vX.Y.Z"),
        ("1.2.3", "is not of the form vX.Y.Z"),
        ("v1.2.3rc1", "is not of the form vX.Y.Z"),
        ("v1.2.3-extra", "is not of the form vX.Y.Z"),
        ("vv1.2.3", "is not of the form vX.Y.Z"),
    ],
)
def test_a_tag_that_isnt_the_version_fails(
    checker: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tag: str,
    message: str,
) -> None:
    pyproject, init = _package(tmp_path, "1.2.3", "1.2.3")
    args = ["--tag", tag, "--pyproject", str(pyproject), "--init", str(init)]
    assert checker.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


@pytest.mark.parametrize("tag", [None, "v1.2.3"])
def test_the_two_version_files_must_agree(
    checker: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], tag: str | None
) -> None:
    pyproject, init = _package(tmp_path, "1.2.3", "1.2.2")
    args = ["--pyproject", str(pyproject), "--init", str(init)]
    if tag is not None:
        args += ["--tag", tag]
    assert checker.main(args) == 1
    err = capsys.readouterr().err
    assert "pyproject.toml says 1.2.3 but netkeep/__init__.py says 1.2.2" in err


def test_the_repository_versions_agree(checker: ModuleType) -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]["version"]
    assert checker.main([]) == 0
    assert project == netkeep.__version__
    assert checker.main(["--tag", f"v{project}"]) == 0


# --- the constraints file and the Macs ------------------------------------------------


def test_the_constraints_file_is_the_documented_export(
    jobs: dict[str, dict[str, Any]],
) -> None:
    build = _runs(jobs["build"])
    assert f"{_EXPORT} --quiet -o dist/constraints.txt" in build
    # Both sides of the cryptography pair must come out of the export, with markers.
    assert "^cryptography==48\\." in build
    assert "platform_machine == 'x86_64' and sys_platform == 'darwin'" in build
    assert "platform_machine != 'x86_64' or sys_platform != 'darwin'" in build
    assert f"{_EXPORT} --quiet -o dist/constraints.txt" in _runs(_load(CI)["jobs"]["wheel"])


def test_pyproject_carries_the_cryptography_pair() -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    assert "cryptography<49; sys_platform == 'darwin' and platform_machine == 'x86_64'" in deps
    assert "cryptography>=49; sys_platform != 'darwin' or platform_machine != 'x86_64'" in deps


def test_the_lock_forks_cryptography() -> None:
    with (ROOT / "uv.lock").open("rb") as f:
        packages = tomllib.load(f)["package"]
    versions = sorted(p["version"] for p in packages if p["name"] == "cryptography")
    assert len(versions) == 2
    intel, rest = versions
    assert intel.startswith("48.")
    assert int(rest.split(".")[0]) >= 49


@pytest.mark.parametrize("workflow", [RELEASE, CI], ids=["release", "ci"])
def test_the_wheel_installs_on_an_intel_and_an_apple_silicon_mac(workflow: Path) -> None:
    found = _load(workflow)["jobs"]
    (job,) = [j for j in found.values() if "scripts/smoke-tool-install.sh" in _runs(j)]
    runners = {entry["runner"] for entry in job["strategy"]["matrix"]["include"]}
    assert runners == {"macos-15-intel", "macos-15"}
    assert job["runs-on"] == "${{ matrix.runner }}"
    assert job["strategy"]["fail-fast"] is False


def test_the_install_smoke_is_a_pinned_uv_tool_install_on_scratch_paths() -> None:
    script = (ROOT / "scripts" / "smoke-tool-install.sh").read_text(encoding="utf-8")
    assert 'uv tool install --python 3.12 --managed-python -c "$constraints" "$wheel"' in script
    for variable in ("HOME", "NETKEEP_DATA", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR"):
        assert f'export {variable}="$work/' in script, variable
    assert '"$netkeep" version' in script
    assert '"$netkeep" config show' in script
    assert '"$netkeep" db upgrade' in script


def test_the_install_smoke_checks_the_cryptography_side_for_the_machine() -> None:
    script = (ROOT / "scripts" / "smoke-tool-install.sh").read_text(encoding="utf-8")
    assert 'if [ "$(uname -s)" = Darwin ] && [ "$machine" = x86_64 ]; then' in script
    assert '[ "$crypto_major" -lt 49 ] ||' in script
    assert '[ "$crypto_major" -ge 49 ] ||' in script


# --- PyObjC, macOS only (ADR 0014 rule 11; #764) --------------------------------------

_PYOBJC = ("pyobjc-core", "pyobjc-framework-cocoa")


def test_pyproject_carries_pyobjc_for_macos_only() -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    assert [d for d in deps if d.lower().startswith("pyobjc")] == [
        "pyobjc-framework-Cocoa>=12.2.2; sys_platform == 'darwin'"
    ]


def test_the_lock_has_a_wheel_for_both_macs_for_each_pyobjc_package() -> None:
    # An install compiles nothing on either Mac (ADR 0011): every PyObjC package has a
    # CPython 3.12 wheel that runs on x86_64 and on arm64, which universal2 is.
    with (ROOT / "uv.lock").open("rb") as f:
        lock = tomllib.load(f)
    packages = {p["name"]: p for p in lock["package"] if p["name"].startswith("pyobjc")}
    assert sorted(packages) == sorted(_PYOBJC)
    for name, package in packages.items():
        wheels = [w["url"].rsplit("/", 1)[1] for w in package["wheels"]]
        cp312 = [w for w in wheels if "-cp312-cp312-macosx_" in w]
        assert any(w.endswith("_universal2.whl") for w in cp312) or (
            any(w.endswith("_x86_64.whl") for w in cp312)
            and any(w.endswith("_arm64.whl") for w in cp312)
        ), (name, wheels)
    (netkeep_package,) = [p for p in lock["package"] if p["name"] == "netkeep"]
    marked = {d["name"]: d.get("marker") for d in netkeep_package["dependencies"]}
    assert marked["pyobjc-framework-cocoa"] == "sys_platform == 'darwin'"


def test_the_release_checks_pyobjc_is_darwin_only_in_the_constraints_file(
    jobs: dict[str, dict[str, Any]],
) -> None:
    build = _runs(jobs["build"])
    for name in _PYOBJC:
        assert f"grep -Ex \"{name}==[0-9.]+ ; sys_platform == 'darwin'\"" in build, name
    assert "grep -vE \" ; sys_platform == 'darwin'$\"" in build


def test_the_install_smoke_imports_appkit_on_macos_only() -> None:
    script = (ROOT / "scripts" / "smoke-tool-install.sh").read_text(encoding="utf-8")
    assert "import AppKit, Foundation, objc" in script
    assert "elif \"$python\" -I -c 'import objc' 2>/dev/null; then" in script
