#!/usr/bin/env bash
# Install a built wheel into a clean venv, start `netkeeper serve` on scratch data and an
# unused port, fetch / and stop the server (#596). Proves the wheel ships the UI.
#   scripts/smoke-wheel.sh [path/to/netkeeper-*.whl]   (default: the one in dist/)
set -euo pipefail

wheel="${1:-$(ls dist/netkeeper-*.whl | head -n1)}"
work="$(mktemp -d)"
server_pid=""
cleanup() {
  [ -n "$server_pid" ] && kill "$server_pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

uv venv -q "$work/venv"
uv pip install -q --python "$work/venv/bin/python" "$wheel"

port="$("$work/venv/bin/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
export NETKEEPER_DATA="$work/data"
# A checkout's frontend/dist and the env override must not be what answers.
unset NETKEEPER_FRONTEND_DIST

# Run from outside the checkout so nothing resolves against the source tree.
cd "$work"
"$work/venv/bin/netkeeper" serve --host 127.0.0.1 --port "$port" >"$work/serve.log" 2>&1 &
server_pid=$!

page=""
for _ in $(seq 1 60); do
  if page="$(curl -fsS "http://127.0.0.1:$port/" 2>/dev/null)"; then break; fi
  kill -0 "$server_pid" 2>/dev/null || { echo "serve exited early:"; cat "$work/serve.log"; exit 1; }
  sleep 1
done

if ! printf '%s' "$page" | grep -q '<div id="root"'; then
  echo "GET / did not return the packaged UI:"; printf '%s\n' "$page" | head -20
  cat "$work/serve.log"; exit 1
fi
if printf '%s' "$page" | grep -q 'frontend is not built'; then
  echo "GET / returned the not-built page"; exit 1
fi
asset="$(printf '%s' "$page" | grep -o '/assets/[^"]*\.js' | head -n1)"
curl -fsS -o /dev/null "http://127.0.0.1:$port$asset"
echo "ok: the wheel serves the UI on port $port ($asset)"
