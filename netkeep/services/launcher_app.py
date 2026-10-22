"""The launcher app: ``~/Applications/netkeep.app``, an icon that opens netkeep (#754).

``netkeep setup`` builds it, ``netkeep update --finish`` rebuilds it, Settings (Run at
login) shows it with a Rebuild button, and ``netkeep setup --remove`` removes it. macOS
only. Design notes, and why Gatekeeper doesn't apply: ``docs/design/install-options.md``.

- **The bundle.** ``Contents/Info.plist`` (:data:`BUNDLE_ID`, the name, the version,
  :data:`MIN_MACOS`), ``Contents/Resources/netkeep.icns`` (the committed icon,
  ``scripts/make-launcher-icon.sh``; left out if the package lacks it) and the executable
  ``Contents/MacOS/netkeep``, a shell script. Nothing else, and nothing downloaded.
- **The script** exports the data directory and the settings the login item's plist
  carries, then runs ``<installed netkeep> open --launcher``, with that absolute path
  written in: the stable installed executable the login item runs (ADR 0011, rule 6;
  :func:`netkeep.services.login_item.installed_executable`). When that fails, or the
  executable is gone, it shows what ``open --launcher`` printed in a plain dialog
  (``/usr/bin/osascript``). It runs nothing else.
- **open --launcher** (:func:`reach_serve`): when ``serve`` doesn't answer, it starts the
  login item (``launchctl kickstart``, or ``bootstrap`` when launchd hasn't loaded it),
  waits for ``/health``, then opens the window the way ``netkeep open`` does.
- **One per data directory,** like the login item's label: ``netkeep.app`` for the default
  one, ``netkeep <hash>.app`` (bundle id ``fun.tnkr.netkeep.launcher.<hash>``) for any other,
  so a scratch data directory never replaces the launcher of your real one.
- **Placement.** Only ``~/Applications``, never through a symbolic link, no ``sudo``. A
  build writes a fresh bundle beside the old one and swaps it in whole. A bundle at that
  path whose bundle id isn't ours is never replaced or removed.

Nothing here touches ``~/Applications`` except through the ``applications_dir`` a
:class:`Launcher` is given; ``tests/conftest.py`` makes the default raise.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from netkeep import __version__, paths
from netkeep.services import keychain
from netkeep.services.login_item import SERVE_ERR_LOG, LoginItem, LoginItemError

BUNDLE_ID: Final = "fun.tnkr.netkeep.launcher"
APP_NAME: Final = "netkeep"
MIN_MACOS: Final = "14.0"
"""The oldest macOS netkeep supports (README, architecture N1)."""
EXECUTABLE: Final = "netkeep"
"""``Contents/MacOS/netkeep``: the script."""
ICON: Final = "netkeep.icns"
OSASCRIPT: Final = "/usr/bin/osascript"
OPEN_ARGUMENTS: Final = ("open", "--launcher")
"""What the script runs the installed ``netkeep`` with."""
UNSUPPORTED_REASON: Final = "The launcher app is a macOS app; on Linux, open netkeep's address."

_NETKEEP_LINE: Final = "netkeep="
_DATA_LINE: Final = f"export {paths.DATA_ENV}="
_DIR_MODE: Final = 0o755
_FILE_MODE: Final = 0o644
_SCRIPT_MODE: Final = 0o755
_POLL_S: Final = 0.5


class LauncherError(RuntimeError):
    """The launcher app could not be built or removed; the message says why."""


def default_applications_dir() -> Path:
    return Path.home() / "Applications"


def bundle_name(data: Path) -> str:
    """``netkeep.app`` for the default data directory, ``netkeep <hash>.app`` for another."""
    if paths.is_default_data_dir(data):
        return f"{APP_NAME}.app"
    return f"{APP_NAME} {paths.data_dir_digest(data)}.app"


def bundle_id(data: Path) -> str:
    if paths.is_default_data_dir(data):
        return BUNDLE_ID
    return f"{BUNDLE_ID}.{paths.data_dir_digest(data)}"


def packaged_icon() -> bytes | None:
    """The committed icon (``netkeep/assets/netkeep.icns``), or ``None`` without it."""
    try:
        return (Path(__file__).resolve().parent.parent / "assets" / ICON).read_bytes()
    except OSError:
        return None


def shell_quote(value: str) -> str:
    """``value`` as one single-quoted ``sh`` word."""
    return "'" + value.replace("'", "'\\''") + "'"


def _shell_unquote(word: str) -> str | None:
    if len(word) < 2 or not (word.startswith("'") and word.endswith("'")):
        return None
    return word[1:-1].replace("'\\''", "'")


DIALOG_SCRIPT: Final = (
    "on run argv",
    'display dialog (item 1 of argv) with title "netkeep" buttons {"OK"}'
    ' default button "OK" with icon caution',
    "end run",
)
"""The AppleScript that shows the message, handed it as an argument, never as code."""

NOT_INSTALLED: Final = (
    "netkeep isn't installed at $netkeep any more. Install it again with the installer"
    " in netkeep's README, then run netkeep setup in Terminal."
)
NO_MESSAGE: Final = "netkeep didn't open. Open Terminal and run: netkeep open"


class State(StrEnum):
    """The launcher's state, in the words the Settings card shows."""

    MISSING = "Not built"
    BUILT = "Built"
    STALE = "Points at a different netkeep"
    NOT_OURS = "Another app is in the way"


@dataclass(frozen=True, slots=True)
class Status:
    supported: bool
    reason: str | None
    path: Path
    exists: bool
    """Something is at :attr:`path`."""
    ours: bool
    """What is there is this data directory's launcher (its bundle id matches)."""
    runs: str | None
    """The ``netkeep`` its script runs."""
    stale: bool
    """Its script runs another ``netkeep`` than the installed one."""

    @property
    def state(self) -> State:
        if not self.exists:
            return State.MISSING
        if not self.ours:
            return State.NOT_OURS
        if self.stale:
            return State.STALE
        return State.BUILT


class Launcher:
    """Build, remove and inspect the launcher app for one data directory."""

    def __init__(
        self,
        *,
        data: Path | None = None,
        applications_dir: Path | None = None,
        executable: Path | None = None,
        macos: bool | None = None,
        environ: Mapping[str, str] | None = None,
        icon: bytes | Callable[[], bytes | None] | None = packaged_icon,
        osascript: str = OSASCRIPT,
        version: str = __version__,
    ) -> None:
        self._data = (data if data is not None else paths.data_dir()).expanduser().resolve()
        self._applications = (
            applications_dir if applications_dir is not None else default_applications_dir()
        )
        self._executable = executable
        self._macos = (sys.platform == "darwin") if macos is None else macos
        self._environ = os.environ if environ is None else environ
        self._icon = icon
        self._osascript = osascript
        self._version = version

    @classmethod
    def for_login_item(cls, item: LoginItem, *, applications_dir: Path | None = None) -> Launcher:
        """The launcher for ``item``'s data directory, running the ``netkeep`` it runs."""
        return cls(data=item.data, executable=item.executable, applications_dir=applications_dir)

    # -- names ------------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._applications / bundle_name(self._data)

    @property
    def executable(self) -> Path | None:
        return self._executable

    # -- what it holds ----------------------------------------------------------------

    def render_info_plist(self, *, icon: bool) -> bytes:
        document: dict[str, object] = {
            "CFBundleIdentifier": bundle_id(self._data),
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": self.path.stem,
            "CFBundleExecutable": EXECUTABLE,
            "CFBundlePackageType": "APPL",
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleShortVersionString": self._version,
            "CFBundleVersion": self._version,
            "LSMinimumSystemVersion": MIN_MACOS,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSHighResolutionCapable": True,
        }
        if icon:
            document["CFBundleIconFile"] = ICON
        return plistlib.dumps(document, fmt=plistlib.FMT_XML)

    def render_script(self) -> str:
        if self._executable is None:
            raise LauncherError(
                "can't find the installed netkeep command for the launcher to run. Install"
                " netkeep with the installer in the README, then run netkeep setup again."
            )
        exports = [f"{_DATA_LINE}{shell_quote(str(self._data))}"]
        # The settings the login item's plist carries, so the launcher finds the same
        # Keychain service and config file as the serve it opens.
        for name in (keychain.SERVICE_ENV, paths.CONFIG_ENV):
            value = (paths.getenv(name, self._environ) or "").strip()
            if value and name == paths.CONFIG_ENV:
                value = str(Path(value).expanduser().resolve())
            if value:
                exports.append(f"export {name}={shell_quote(value)}")
        dialog = " ".join(f"-e {shell_quote(line)}" for line in DIALOG_SCRIPT)
        arguments = " ".join(OPEN_ARGUMENTS)
        lines = [
            "#!/bin/sh",
            "# netkeep's launcher app (#754), written by netkeep setup; netkeep update rewrites",
            "# it. It opens netkeep for one data directory, or says in a dialog why it can't.",
            *exports,
            f"{_NETKEEP_LINE}{shell_quote(str(self._executable))}",
            'if [ -x "$netkeep" ]; then',
            f'  message=$("$netkeep" {arguments}) && exit 0',
            "else",
            f'  message="{NOT_INSTALLED}"',
            "fi",
            f'[ -n "$message" ] || message={shell_quote(NO_MESSAGE)}',
            f'{shell_quote(self._osascript)} {dialog} "$message" >/dev/null',
            "exit 1",
        ]
        return "\n".join(lines) + "\n"

    def _icon_bytes(self) -> bytes | None:
        return self._icon() if callable(self._icon) else self._icon

    def _rendered(self) -> dict[str, tuple[bytes, int]]:
        """Each file of the bundle, relative to it: its content and mode."""
        icon = self._icon_bytes()
        files = {
            "Contents/Info.plist": (self.render_info_plist(icon=icon is not None), _FILE_MODE),
            "Contents/PkgInfo": (b"APPL????", _FILE_MODE),
            f"Contents/MacOS/{EXECUTABLE}": (self.render_script().encode(), _SCRIPT_MODE),
        }
        if icon is not None:
            files[f"Contents/Resources/{ICON}"] = (icon, _FILE_MODE)
        return files

    # -- state ------------------------------------------------------------------------

    def status(self) -> Status:
        path = self.path
        if not self._macos:
            return Status(
                supported=False,
                reason=UNSUPPORTED_REASON,
                path=path,
                exists=False,
                ours=False,
                runs=None,
                stale=False,
            )
        exists = path.exists() or path.is_symlink()
        ours = exists and self._is_ours()
        runs = self._script_value(_NETKEEP_LINE) if ours else None
        return Status(
            supported=True,
            reason=None,
            path=path,
            exists=exists,
            ours=ours,
            runs=runs,
            stale=ours and self._executable is not None and runs != str(self._executable),
        )

    def up_to_date(self) -> bool:
        """The bundle on disk is exactly what a build would write now."""
        if not self._is_ours():
            return False
        try:
            wanted = self._rendered()
        except LauncherError:
            return False
        for relative, (content, mode) in wanted.items():
            file = self.path / relative
            try:
                if file.is_symlink() or file.read_bytes() != content:
                    return False
                if file.stat().st_mode & 0o777 != mode:
                    return False
            except OSError:
                return False
        present = {
            str(file.relative_to(self.path))
            for file in self.path.rglob("*")
            if file.is_file() or file.is_symlink()
        }
        return present == set(wanted)

    def _is_ours(self) -> bool:
        path = self.path
        if path.is_symlink() or not path.is_dir():
            return False
        try:
            with (path / "Contents" / "Info.plist").open("rb") as handle:
                document = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return False
        return isinstance(document, dict) and document.get("CFBundleIdentifier") == bundle_id(
            self._data
        )

    def _script_value(self, prefix: str) -> str | None:
        """The value of the script's ``<prefix>'...'`` line."""
        try:
            text = (self.path / "Contents" / "MacOS" / EXECUTABLE).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        for line in text.splitlines():
            if line.startswith(prefix):
                return _shell_unquote(line.removeprefix(prefix))
        return None

    # -- building and removing --------------------------------------------------------

    def build(self) -> str:
        """Write the bundle, replacing this data directory's old one whole. What it did."""
        self._require_macos()
        files = self._rendered()  # raises before anything is written
        target = self._check_target()
        existed = target.exists()
        self._applications.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".netkeep-launcher-", dir=self._applications))
        try:
            staging.chmod(_DIR_MODE)
            for relative, (content, mode) in files.items():
                file = staging / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
                file.chmod(mode)
            for folder in staging.rglob("*"):
                if folder.is_dir():
                    folder.chmod(_DIR_MODE)  # the umask cannot narrow it
            self._swap_in(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        verb = "Rebuilt" if existed else "Built"
        note = "" if files.get(f"Contents/Resources/{ICON}") else " (without its icon)"
        return f"{verb} {target}{note}: it opens netkeep with {self._executable}."

    def _swap_in(self, staging: Path, target: Path) -> None:
        """Replace ``target`` with ``staging`` whole. The old bundle moves aside first and
        comes back if the move in fails."""
        if not target.exists():
            staging.rename(target)
            return
        aside = Path(tempfile.mkdtemp(prefix=".netkeep-launcher-old-", dir=self._applications))
        aside.rmdir()
        target.rename(aside)
        try:
            staging.rename(target)
        except BaseException:
            aside.rename(target)
            raise
        shutil.rmtree(aside, ignore_errors=True)

    def remove(self) -> str:
        """Remove this data directory's launcher. What it did."""
        self._require_macos()
        target = self._check_target()
        if not target.exists():
            return f"already done: there is no {target}."
        shutil.rmtree(target)
        return f"done: removed {target}."

    def _check_target(self) -> Path:
        """:attr:`path`, after refusing a symbolic link on the way or another app there."""
        applications = self._applications
        if applications.is_symlink() or (applications.exists() and not applications.is_dir()):
            raise LauncherError(
                f"{applications} is not a plain folder, so netkeep won't put its launcher there."
            )
        target = self.path
        if target.is_symlink() or (target.exists() and not self._is_ours()):
            raise LauncherError(
                f"{target} is there and isn't netkeep's launcher, so netkeep leaves it alone."
                " Move it out of the way, then try again."
            )
        return target

    def _require_macos(self) -> None:
        if not self._macos:
            raise LauncherError(UNSUPPORTED_REASON)


# -- open --launcher -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServeSeams:
    """What :func:`reach_serve` reaches the world through; the CLI passes the real ones."""

    answers: Callable[[str], bool]
    """Whether a netkeep ``serve`` answers ``/health`` at that origin."""
    lock_held: Callable[[], bool]
    """Whether a ``serve`` holds this data directory's serve lock."""
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def reach_serve(item: LoginItem, base: str, seams: ServeSeams, *, wait_s: float) -> str | None:
    """Make sure ``serve`` answers at ``base`` before the launcher opens its window.

    ``None`` when it answers, if need be after starting the login item; else what the
    dialog says. It never starts a second ``serve``: while one holds the serve lock it
    only waits, and with the login item off it starts nothing."""
    if seams.answers(base):
        return None
    status = item.status()
    if not status.supported:
        return status.reason
    if not seams.lock_held():
        if not status.installed:
            return (
                "netkeep isn't running, and it doesn't start at login. Open Terminal and"
                " run: netkeep setup"
            )
        if status.legacy:
            return (
                "netkeep isn't running, and its login item needs repair. Open Terminal and"
                " run: netkeep setup"
            )
        if status.disabled:
            return (
                f"netkeep isn't running, and launchd has its login item {status.label}"
                " turned off. Open Terminal and run: netkeep setup"
            )
        try:
            item.start()
        except LoginItemError as exc:
            return (
                f"netkeep couldn't start: {exc}. Open Terminal and run: netkeep login-item status"
            )
    deadline = seams.clock() + wait_s
    while not seams.answers(base):
        if seams.clock() >= deadline:
            errors = item.data / "logs" / SERVE_ERR_LOG
            return (
                f"netkeep didn't answer at {base} within {wait_s:g} seconds. {errors} may say"
                " why. Open Terminal and run: netkeep login-item status"
            )
        seams.sleep(_POLL_S)
    return None


def refresh(launcher: Launcher, *, build: bool) -> str | None:
    """``netkeep update --finish``'s step (#754): rebuild the launcher when it is ours and
    not what a build writes now, or build a missing one when ``build``. What it did, or
    ``None`` when it did nothing. A failure is reported, never raised: the update itself
    went through."""
    status = launcher.status()
    if not status.supported or (status.exists and not status.ours):
        return None
    if not status.exists and not build:
        return None
    if status.exists and launcher.up_to_date():
        return None
    try:
        return launcher.build()
    except (LauncherError, OSError) as exc:
        return (
            f"not rebuilt: {exc}. Rebuild it in Settings, under Run at login, or run netkeep setup."
        )
