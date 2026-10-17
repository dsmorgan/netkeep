#!/bin/sh
# What the fun.tnkr.netkeeper LaunchAgent runs: apply migrations, then replace
# this shell with `netkeeper serve`. Any arguments are passed through to serve.
#
# serve migrates at startup too; the explicit upgrade is belt and braces and
# leaves a readable line in the log before the server starts.
set -eu

# Follow symlinks to the script's real location so the repo root is right when
# the script is invoked through a link. Relative link targets resolve against
# the link's directory. Bounded so a symlink loop cannot hang us.
resolve_script_path() {
  rsp_path=$1
  rsp_hops=0
  while [ -L "$rsp_path" ] && [ "$rsp_hops" -lt 40 ]; do
    rsp_target=$(readlink "$rsp_path")
    case $rsp_target in
      /*) rsp_path=$rsp_target ;;
      *) rsp_path=$(dirname "$rsp_path")/$rsp_target ;;
    esac
    rsp_hops=$((rsp_hops + 1))
  done
  printf '%s\n' "$rsp_path"
}

script_dir=$(cd "$(dirname "$(resolve_script_path "$0")")" && pwd -P)
repo_root=$(cd "$script_dir/.." && pwd -P)
netkeeper=${NETKEEPER_BIN:-$repo_root/.venv/bin/netkeeper}

if [ ! -x "$netkeeper" ]; then
  echo "serve-launchd.sh: $netkeeper is missing; run 'make install' in $repo_root first." >&2
  exit 1
fi

# Under the agent (NETKEEPER_LAUNCHD_LOG_DIR set by install-launchd.sh), stdout and
# stderr are reopened here, after the one over ROTATE_BYTES moves to <name>.1, so
# serve.log and serve.err.log never grow without bound. launchd's own handles stay on
# the moved file and nothing writes to them after this.
ROTATE_BYTES=5242880
if [ -n "${NETKEEPER_LAUNCHD_LOG_DIR:-}" ]; then
  mkdir -p "$NETKEEPER_LAUNCHD_LOG_DIR"
  for name in serve.log serve.err.log; do
    file="$NETKEEPER_LAUNCHD_LOG_DIR/$name"
    if [ -f "$file" ] && [ "$(wc -c <"$file" | tr -d " ")" -gt "$ROTATE_BYTES" ]; then
      mv -f "$file" "$file.1"
    fi
  done
  exec >>"$NETKEEPER_LAUNCHD_LOG_DIR/serve.log" 2>>"$NETKEEPER_LAUNCHD_LOG_DIR/serve.err.log"
fi

echo "serve-launchd.sh: $(date -u +%Y-%m-%dT%H:%M:%SZ) applying migrations"
"$netkeeper" db upgrade
echo "serve-launchd.sh: $(date -u +%Y-%m-%dT%H:%M:%SZ) starting netkeeper serve $*"
exec "$netkeeper" serve "$@"
