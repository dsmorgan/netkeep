#!/usr/bin/env bash
# Install a built wheel the way a user does (ADR 0011 rule 1): a fresh uv tool
# environment, uv's own Python 3.12, and the release's constraints file. Then run
# commands that touch no network, on a scratch HOME and NETKEEP_DATA (#683).
#   scripts/smoke-tool-install.sh <dist-dir>
# <dist-dir> holds netkeep-X.Y.Z-py3-none-any.whl and constraints.txt.
set -euo pipefail

dist="${1:?usage: scripts/smoke-tool-install.sh <dist-dir>}"
dist="$(cd "$dist" && pwd)"
wheel="$(ls "$dist"/netkeep-*-py3-none-any.whl)"
constraints="$dist/constraints.txt"
test -f "$constraints" || { echo "no constraints.txt in $dist"; exit 1; }
# netkeep-0.1.0-py3-none-any.whl -> 0.1.0
expected="$(basename "$wheel" | cut -d- -f2)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# Everything this install writes stays under $work: the tool environment, its command,
# the interpreter uv downloads, and netkeep's data. HOME too, so the rename's first-start
# migration (#661) finds no real data folder to look at.
export HOME="$work/home"
export NETKEEP_DATA="$work/data"
export UV_TOOL_DIR="$work/tools"
export UV_TOOL_BIN_DIR="$work/bin"
export UV_PYTHON_INSTALL_DIR="$work/python"
unset NETKEEP_CONFIG NETKEEP_FRONTEND_DIST
mkdir -p "$HOME"
cd "$work"

uv tool install --python 3.12 --managed-python -c "$constraints" "$wheel"
netkeep="$UV_TOOL_BIN_DIR/netkeep"
python="$UV_TOOL_DIR/netkeep/bin/python"

got="$("$netkeep" version)"
if [ "$got" != "$expected" ]; then
  echo "netkeep version printed $got, expected $expected"; exit 1
fi

# The interpreter is the machine's own architecture (no Rosetta on Intel, no x86_64
# Python on Apple silicon), and cryptography is the side of the pair it should be.
machine="$(uname -m)"
read -r py_machine py_version crypto < <("$python" -c '
import platform, cryptography
print(platform.machine(), platform.python_version(), cryptography.__version__)')
echo "machine $machine, Python $py_version ($py_machine), cryptography $crypto"
[ "$py_machine" = "$machine" ] || { echo "the interpreter is $py_machine on a $machine machine"; exit 1; }
case "$py_version" in 3.12.*) ;; *) echo "expected Python 3.12, got $py_version"; exit 1 ;; esac
crypto_major="${crypto%%.*}"
if [ "$(uname -s)" = Darwin ] && [ "$machine" = x86_64 ]; then
  [ "$crypto_major" -lt 49 ] || { echo "Intel Mac got cryptography $crypto, expected <49"; exit 1; }
else
  [ "$crypto_major" -ge 49 ] || { echo "got cryptography $crypto, expected >=49"; exit 1; }
fi

# PyObjC, for the menu-bar app (ADR 0014 rule 11; #764): installed from its universal2
# wheel on either Mac, so AppKit and Foundation import in the machine's own interpreter.
# Anywhere else the darwin marker leaves it out.
if [ "$(uname -s)" = Darwin ]; then
  "$python" -I -c '
import AppKit, Foundation, objc
print("PyObjC", objc.__version__, AppKit.NSApplication.__name__, Foundation.NSBundle.__name__)'
elif "$python" -I -c 'import objc' 2>/dev/null; then
  echo "PyObjC is installed on $(uname -s), expected only on macOS"; exit 1
fi

# Offline commands: the resolved settings and paths, then the packaged migrations on an
# empty scratch database.
"$netkeep" config show >"$work/config.txt"
grep -qF "data_dir = \"$NETKEEP_DATA\"" "$work/config.txt" || {
  echo "config show did not use the scratch data directory:"; cat "$work/config.txt"; exit 1
}
"$netkeep" db upgrade
"$netkeep" db current

# The launcher app (#754), built by the installed package into the scratch HOME the way
# netkeep setup builds it (without setup's login item and Chrome). On macOS only: the
# bundle's plist must lint, its script must parse, run the uv shim and be executable,
# the icon must have shipped in the wheel, and nothing may carry a quarantine attribute,
# which is why Gatekeeper's first-run check doesn't apply (docs/design/install-options.md).
if [ "$(uname -s)" = Darwin ]; then
  "$python" -I -c '
from netkeep.services import launcher_app, login_item
print(launcher_app.Launcher.for_login_item(login_item.LoginItem()).build())'
  bundle="$(ls -d "$HOME/Applications/"netkeep*.app)"
  plutil -lint "$bundle/Contents/Info.plist"
  [ "$(plutil -extract CFBundleIdentifier raw "$bundle/Contents/Info.plist")" = \
    "fun.tnkr.netkeep.launcher.$("$python" -I -c 'from netkeep import paths; print(paths.data_dir_digest(paths.data_dir()))')" ] \
    || { echo "the launcher's bundle id is wrong"; exit 1; }
  [ "$(plutil -extract CFBundleShortVersionString raw "$bundle/Contents/Info.plist")" = "$expected" ] \
    || { echo "the launcher's version is not $expected"; exit 1; }
  script="$bundle/Contents/MacOS/netkeep"
  [ -x "$script" ] || { echo "the launcher's script is not executable"; exit 1; }
  sh -n "$script"
  grep -qxF "netkeep='$netkeep'" "$script" || { echo "the launcher doesn't run $netkeep:"; cat "$script"; exit 1; }
  [ "$(head -c 4 "$bundle/Contents/Resources/netkeep.icns")" = icns ] || { echo "no icon in the launcher"; exit 1; }
  if xattr -r "$bundle" | grep -q com.apple.quarantine; then
    echo "the launcher carries a quarantine attribute:"; xattr -lr "$bundle"; exit 1
  fi
  echo "ok: the launcher app builds on macOS $(sw_vers -productVersion), with no quarantine attribute"
fi
echo "ok: netkeep $got installs as a uv tool with the constraints file on $machine"
