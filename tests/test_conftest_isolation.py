"""The autouse fixtures in ``conftest.py`` keep the developer's machine out of a test."""

from __future__ import annotations

import subprocess
from pathlib import Path

from netkeep.config import load_settings
from netkeep.paths import config_candidates


def test_every_test_runs_in_its_own_directory(tmp_path: Path) -> None:
    """#703: ``./config.toml`` is the first config looked for, so a test run from a
    checkout holding one must not find it. The fixture moves each test, and each
    script it runs, into ``tmp_path``."""
    assert Path.cwd().resolve() == tmp_path.resolve()
    pwd = subprocess.run(["/bin/pwd", "-P"], capture_output=True, text=True, check=True)
    assert Path(pwd.stdout.strip()) == tmp_path.resolve()
    assert (tmp_path / "config.toml").resolve() in [p.resolve() for p in config_candidates()]


def test_a_config_in_the_working_directory_is_this_tests_own(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[logs]\n")
    source = load_settings().source_path
    assert source is not None
    assert source.resolve() == (tmp_path / "config.toml").resolve()
