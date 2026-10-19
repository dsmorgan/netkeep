"""The Keychain service is per data directory (#580).

Every fresh database has user 1 and mailbox 1, so two data directories under one
service would read, overwrite and delete each other's Gmail client and token. The
default data directory keeps ``netkeep``, so an existing install keeps its secrets.
The keyring is conftest's in-memory fake; no test here touches a real Keychain, and
the default data directory is under a scratch ``HOME``.
"""

import re
import sys
from pathlib import Path

import factories
import pytest
from gmail_fakes import FAKE_EMAIL, MemoryKeyring
from sqlalchemy.orm import Session, sessionmaker

from netkeep import paths
from netkeep.db import session_scope
from netkeep.services import keychain
from netkeep.services import mailboxes as service

PACKAGE = Path(__file__).resolve().parents[1] / "netkeep"
TOKEN = "gmail/mailbox/1"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A scratch ``HOME`` on macOS, so the default data directory is never the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "platform", "darwin")
    return home


def _default(home: Path) -> Path:
    return home / "Library" / "Application Support" / "netkeep"


def _use(monkeypatch: pytest.MonkeyPatch, directory: str | Path | None) -> None:
    if directory is None:
        monkeypatch.delenv(paths.DATA_ENV, raising=False)
    else:
        monkeypatch.setenv(paths.DATA_ENV, str(directory))


def test_the_default_data_directory_keeps_the_service_existing_installs_used(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    _use(monkeypatch, None)
    assert keychain.service_name() == keychain.DEFAULT_SERVICE == "netkeep"


@pytest.mark.parametrize("create", [False, True])
def test_naming_the_default_directory_explicitly_still_uses_netkeep(
    monkeypatch: pytest.MonkeyPatch, home: Path, create: bool
) -> None:
    """``NETKEEP_DATA`` set to the default path, however it is spelled, is the default."""
    default = _default(home)
    if create:
        default.mkdir(parents=True)
    link = home / "nk-link"
    link.symlink_to(default, target_is_directory=True)
    spellings = [
        str(default),
        f"{default}/",
        "~/Library/Application Support/netkeep",
        str(home / "Library" / ".." / "Library" / "Application Support" / "netkeep"),
        str(link),
    ]
    for spelling in spellings:
        _use(monkeypatch, spelling)
        assert keychain.service_name() == "netkeep", spelling


def test_a_case_insensitive_spelling_of_an_existing_default_is_the_default(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    default = _default(home)
    default.mkdir(parents=True)
    other_case = home / "library" / "application support" / "NETKEEP"
    if not other_case.exists():
        pytest.skip("this file system is case-sensitive")
    _use(monkeypatch, other_case)
    assert keychain.service_name() == "netkeep"


def test_off_macos_the_default_is_data_under_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    for directory in (None, "data", tmp_path / "data"):
        _use(monkeypatch, directory)
        assert keychain.service_name() == "netkeep", directory
    _use(monkeypatch, tmp_path / "elsewhere")
    assert keychain.service_name() != "netkeep"


def test_any_other_directory_gets_its_own_stable_hashed_service(
    monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path
) -> None:
    _use(monkeypatch, tmp_path / "one")
    one = keychain.service_name()
    assert re.fullmatch(r"netkeep@[0-9a-f]{12}", one)
    _use(monkeypatch, f"{tmp_path}/two/../one/")
    assert keychain.service_name() == one  # the resolved path decides
    _use(monkeypatch, tmp_path / "two")
    assert keychain.service_name() not in {one, "netkeep"}
    # A directory beside the default, or inside it, is not the default.
    for near in (_default(home) / "sub", _default(home).with_name("netkeep-2")):
        _use(monkeypatch, near)
        assert keychain.service_name() != "netkeep", near


def test_the_override_names_the_service(
    monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv(keychain.SERVICE_ENV, " netkeep-rehearsal ")
    for directory in (None, tmp_path / "scratch"):
        _use(monkeypatch, directory)
        assert keychain.service_name() == "netkeep-rehearsal"
    monkeypatch.setenv(keychain.SERVICE_ENV, "  ")  # blank is unset
    assert keychain.service_name().startswith("netkeep@")


def test_the_default_directory_still_reads_its_existing_entries(
    monkeypatch: pytest.MonkeyPatch, home: Path, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.entries[("netkeep", f"1/{TOKEN}")] = "rt-existing"
    _use(monkeypatch, None)
    assert keychain.get_secret(1, TOKEN) == "rt-existing"
    _use(monkeypatch, _default(home))
    assert keychain.get_secret(1, TOKEN) == "rt-existing"


def test_two_data_directories_never_share_an_entry(
    monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    _use(monkeypatch, None)
    keychain.set_secret(1, TOKEN, "rt-live")
    _use(monkeypatch, tmp_path / "scratch")
    assert keychain.get_secret(1, TOKEN) is None  # the scratch install never sees it
    keychain.set_secret(1, TOKEN, "rt-scratch")
    assert keychain.get_secret(1, TOKEN) == "rt-scratch"
    _use(monkeypatch, None)
    assert keychain.get_secret(1, TOKEN) == "rt-live"  # and never overwrites it
    services = {entry_service for entry_service, _ in memory_keyring.entries}
    assert len(services) == 2 and "netkeep" in services


def test_disconnect_in_one_directory_never_deletes_the_others_token(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    memory_keyring: MemoryKeyring,
) -> None:
    _use(monkeypatch, None)
    keychain.set_secret(1, TOKEN, "rt-live")  # the live install's user 1, mailbox 1

    _use(monkeypatch, tmp_path / "scratch")
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = service.connect(session, user, FAKE_EMAIL, "rt-scratch", daily_cap=80)
        assert (user.id, mailbox.keychain_ref) == (1, TOKEN)  # the same entry name
        service.disconnect(session, user, mailbox)
    assert keychain.get_secret(1, TOKEN) is None

    _use(monkeypatch, None)
    assert keychain.get_secret(1, TOKEN) == "rt-live"
    assert keychain.delete_secret(1, "gmail/oauth_client") is False


def test_only_the_keychain_module_calls_keyring() -> None:
    """Every secret goes through :func:`keychain.service_name`, so one module imports it."""
    importers = [
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if re.search(r"^\s*(import|from)\s+keyring\b", path.read_text(), re.MULTILINE)
    ]
    assert importers == ["services/keychain.py"]


def test_a_literal_service_name_for_one_path_never_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing the hash would silently move everyone's Gmail secrets to a new service
    (and, since #600, rename their login item's label)."""
    monkeypatch.setenv("NETKEEP_DATA", "/nk-pin/data")
    assert keychain.service_name() == "netkeep@6f70bec19518"
