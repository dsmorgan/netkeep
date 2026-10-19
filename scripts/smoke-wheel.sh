#!/usr/bin/env bash
# Install a built wheel into a clean venv, start `netkeep serve` on scratch data and an
# unused port, fetch / and stop the server (#596). Proves the wheel ships the UI.
#   scripts/smoke-wheel.sh [path/to/netkeep-*.whl]   (default: the one in dist/)
set -euo pipefail

wheel="${1:-$(ls dist/netkeep-*.whl | head -n1)}"
work="$(mktemp -d)"
server_pid=""
cleanup() {
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
    # serve still writes its log while it shuts down: wait (about 10 s at most) before
    # removing the directory under it.
    for _ in $(seq 1 20); do
      kill -0 "$server_pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

uv venv -q "$work/venv"
uv pip install -q --python "$work/venv/bin/python" "$wheel"

port="$("$work/venv/bin/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
export NETKEEP_DATA="$work/data"
# A checkout's frontend/dist and the env override must not be what answers.
unset NETKEEP_FRONTEND_DIST

# Run from outside the checkout so nothing resolves against the source tree.
cd "$work"
"$work/venv/bin/netkeep" serve --host 127.0.0.1 --port "$port" >"$work/serve.log" 2>&1 &
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
