import re
import sys
from pathlib import Path

import pytest

from netkeep import paths
from netkeep.paths import config_candidates, data_dir, ensure_data_dir


def test_data_dir_env_var_wins_over_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NETKEEP_DATA", str(tmp_path / "custom"))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_dir() == tmp_path / "custom"


def test_data_dir_env_var_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NETKEEP_DATA", "~/nk")
    assert data_dir() == tmp_path / "nk"


def test_data_dir_empty_env_var_is_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NETKEEP_DATA", "")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    assert data_dir() == tmp_path / "data"


def test_data_dir_macos_uses_application_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NETKEEP_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_dir() == tmp_path / "Library" / "Application Support" / "netkeep"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_data_dir_other_platforms_use_cwd_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    monkeypatch.delenv("NETKEEP_DATA", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.chdir(tmp_path)
    assert data_dir() == tmp_path / "data"


def test_data_dir_does_not_create_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NETKEEP_DATA", str(tmp_path / "fresh"))
    assert not data_dir().exists()
    assert not config_candidates(None)[-1].parent.exists()


def test_ensure_data_dir_creates_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "dir"
    monkeypatch.setenv("NETKEEP_DATA", str(target))
    assert ensure_data_dir() == target
    assert target.is_dir()
    assert ensure_data_dir() == target  # already exists: no error


def test_config_candidates_full_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NETKEEP_CONFIG", str(tmp_path / "env.toml"))
    monkeypatch.setenv("NETKEEP_DATA", str(tmp_path / "data"))
    explicit = tmp_path / "explicit.toml"
    assert config_candidates(explicit) == [
        explicit,
        tmp_path / "env.toml",
        cwd / "config.toml",
        tmp_path / "data" / "config.toml",
    ]


def test_config_candidates_without_explicit_or_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETKEEP_CONFIG", "")
    monkeypatch.setenv("NETKEEP_DATA", str(tmp_path / "data"))
    assert config_candidates(None) == [
        tmp_path / "config.toml",
        tmp_path / "data" / "config.toml",
    ]


def test_config_candidates_env_var_expands_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NETKEEP_CONFIG", "~/nk.toml")
    assert config_candidates(None)[0] == tmp_path / "nk.toml"


def test_config_candidates_explicit_expands_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_candidates(Path("~/nk.toml"))[0] == tmp_path / "nk.toml"


# --- the names before the rename (#661) ------------------------------------------------


def test_the_old_variable_still_names_the_data_directory_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(paths, "_warned", set())
    monkeypatch.delenv("NETKEEP_DATA", raising=False)
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "old-name"))
    with caplog.at_level("WARNING", logger="netkeep.paths"):
        assert data_dir() == tmp_path / "old-name"
        assert data_dir() == tmp_path / "old-name"
    warnings = [r.getMessage() for r in caplog.records if "old name" in r.getMessage()]
    assert warnings == [
        "NETKEEPER_DATA is the old name of NETKEEP_DATA; rename it, the old name stops"
        " working in the next release"
    ]


def test_the_new_variable_wins_over_the_old(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NETKEEP_DATA", str(tmp_path / "new"))
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "old"))
    assert data_dir() == tmp_path / "new"


@pytest.mark.parametrize(
    "name",
    [
        "NETKEEP_CONFIG",
        "NETKEEP_DATABASE_URL",
        "NETKEEP_FRONTEND_DIST",
        "NETKEEP_KEYCHAIN_SERVICE",
        "NETKEEP_LOG_LEVEL",
        "NETKEEP_LOG_STDERR_LEVEL",
    ],
)
def test_every_setting_reads_its_old_name(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(name.replace("NETKEEP_", "NETKEEPER_"), "from-the-old-name")
    assert paths.getenv(name) == "from-the-old-name"
    monkeypatch.setenv(name, "from-the-new-name")
    assert paths.getenv(name) == "from-the-new-name"


def test_the_fallback_reaches_each_reader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The modules read through :func:`paths.getenv`, not ``os.environ`` directly."""
    from netkeep import db
    from netkeep.services import keychain
    from netkeep.web import frontend

    for name in ("NETKEEP_DATABASE_URL", "NETKEEP_KEYCHAIN_SERVICE", "NETKEEP_FRONTEND_DIST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", "sqlite:///old.sqlite3")
    monkeypatch.setenv("NETKEEPER_KEYCHAIN_SERVICE", "old-service")
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "dist"))
    assert db.database_url() == "sqlite:///old.sqlite3"
    assert keychain.service_name() == "old-service"
    assert frontend.frontend_dist() == tmp_path / "dist"


def test_no_module_reads_a_netkeep_variable_around_the_fallback() -> None:
    """Every ``NETKEEP_*`` read goes through :func:`paths.getenv` (#661)."""
    package = Path(paths.__file__).parent
    offenders = [
        f"{path.relative_to(package)}:{number}"
        for path in sorted(package.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"os\.(environ\.get|getenv)\(\s*[\w.]*_ENV\b", line)
        or re.search(r"os\.(environ\.get|getenv)\(\s*\"NETKEEP", line)
    ]
    assert offenders == []
