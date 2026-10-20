"""``netkeep.fsutil.write_atomic`` (#705): a reader sees the old file or the new one."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from netkeep.fsutil import write_atomic


def test_write_atomic_replaces_the_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("old\n")
    write_atomic(target, "new\n")
    assert target.read_text() == "new\n"
    write_atomic(target, b"\x00bytes")
    assert target.read_bytes() == b"\x00bytes"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_write_atomic_sets_the_mode(tmp_path: Path) -> None:
    private, shared = tmp_path / "private", tmp_path / "shared"
    write_atomic(private, "x")
    write_atomic(shared, "x", mode=0o644)
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(shared.stat().st_mode) == 0o644


def test_a_failed_write_leaves_the_old_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "constraints.txt"
    target.write_text("kept\n")

    def full(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("os.fsync", full)
    with pytest.raises(OSError, match="No space left"):
        write_atomic(target, "half")
    assert target.read_text() == "kept\n"
    assert [p.name for p in tmp_path.iterdir()] == ["constraints.txt"]


def test_each_write_uses_its_own_temporary_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writers of one file never share a temporary file, so neither renames the
    other's half-written one into place."""
    seen: list[str] = []
    real_replace = Path.replace

    def replace(self: Path, target: Path) -> Path:
        seen.append(self.name)
        assert self.parent == tmp_path, "the temporary file is in the target's folder"
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    write_atomic(tmp_path / "f", "1")
    write_atomic(tmp_path / "f", "2")
    assert len(set(seen)) == 2
    assert all(name.startswith(".f.") and name.endswith(".tmp") for name in seen)
    assert os.listdir(tmp_path) == ["f"]
