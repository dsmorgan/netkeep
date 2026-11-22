#!/bin/sh
# Update this checkout to origin/main and put the new code in service: `make update`
# (#839). It runs every step `git pull`, `make install`, `make build-ui` and a restart of
# serve took by hand, in order, and stops at the first that fails.
#
#   1. Check the checkout: git, uv and pnpm on PATH, .venv/bin/netkeep there, branch
#      main, no merge, cherry-pick, revert or rebase in progress, no uncommitted changes
#      to tracked files, then fetch origin main and check main fast-forwards to it. A
#      failed check changes nothing.
#   2. `netkeep update --checkout-stop`, on the code the checkout has now: it refuses
#      while you run serve yourself, stops the login item, and backs up the database.
#   3. Fast-forward main to what step 1 fetched.
#   4. uv sync --all-extras --frozen.
#   5. Build the UI into frontend/dist (what `make build-ui` runs).
#   6. `netkeep update --checkout-start`, on the new code: it starts the login item and
#      checks that serve answers, or, with the login item off, says to start serve.
#
# There is no automatic rollback of git: a failure prints where things stand and what
# to run next. Running `make update` again after a fix finishes the update.
set -eu
# pipefail is not POSIX; turn it on where the shell has it (bash, zsh, newer dash).
(set -o pipefail) 2>/dev/null && set -o pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/update-checkout.sh   (or: make update)

Update this checkout to origin/main: stop netkeep serve and back up the database,
fast-forward main, uv sync, build the UI, then start serve again and check it
answers. Run it from a checkout on main with no uncommitted changes.
USAGE
}

say() {
  printf '%s\n' "$*"
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

short() {
  git rev-parse --short "$1"
}

# A step after the pull failed: say where things stand and what to run next.
stopped_after_pull() {
  why=$1
  say ""
  say "make update stopped: $why."
  say "Where things stand:"
  say "  - The checkout is at $(short "$new"); it was at $(short "$old"). .venv and the"
  say "    UI may not match it yet."
  say "  - netkeep serve is stopped. Nothing has run on the new code, so the database is"
  say "    as it was before the update."
  say "What to run next:"
  say "  - Fix what failed, then run make update again. It finishes the update and"
  say "    starts the login item again."
  say "  - Or go back to $(short "$old"):"
  say "      git reset --keep $old"
  say "      uv sync --all-extras --frozen"
  say "      make build-ui"
  say "    then start netkeep serve: .venv/bin/netkeep login-item on, or make serve."
  exit 1
}

main() {
  case "${1:-}" in
    -h | --help)
      usage
      exit 0
      ;;
    "") ;;
    *)
      usage >&2
      exit 2
      ;;
  esac

  root=$(cd "$(dirname "$0")/.." && pwd -P)
  cd "$root"
  netkeep=.venv/bin/netkeep

  say "1. Check the checkout"
  for tool in git uv pnpm; do
    command -v "$tool" >/dev/null 2>&1 \
      || die "$tool isn't on PATH. Install it (README, From a checkout), then run make update again."
  done
  top=$(git rev-parse --show-toplevel 2>/dev/null) || die "$root isn't a git checkout."
  [ "$(cd "$top" && pwd -P)" = "$root" ] || die "$root isn't the top of a git checkout."
  [ -x "$netkeep" ] || die "there is no $netkeep. Set the checkout up first with: make setup"
  branch=$(git symbolic-ref --quiet --short HEAD) \
    || die "HEAD isn't on a branch. Run git switch main, then make update."
  [ "$branch" = main ] \
    || die "this checkout is on $branch, not main. Run git switch main, then make update."
  for pending in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-apply rebase-merge; do
    [ ! -e "$(git rev-parse --git-path "$pending")" ] \
      || die "a git merge, cherry-pick, revert or rebase is in progress ($pending). Finish or abort it, then run make update."
  done
  [ -z "$(git status --porcelain --untracked-files=no)" ] \
    || die "the checkout has uncommitted changes (git status). Commit or discard them, then run make update."
  git fetch --quiet origin main || die "git fetch origin main failed. Nothing was changed."
  old=$(git rev-parse --verify HEAD)
  new=$(git rev-parse --verify 'FETCH_HEAD^{commit}')
  git merge-base --is-ancestor "$old" "$new" \
    || die "main has commits origin/main doesn't, so it can't fast-forward. Nothing was changed."
  if [ "$old" = "$new" ]; then
    say "   main is at origin/main ($(short "$new")): nothing to pull, so it reinstalls and"
    say "   rebuilds, and .venv and the UI match the checkout"
  else
    say "   main fast-forwards from $(short "$old") to $(short "$new")"
  fi

  say "2. Stop netkeep serve and back up the database"
  if ! "$netkeep" update --checkout-stop; then
    say ""
    say "make update stopped before changing anything: the checkout, .venv and the UI"
    say "are as they were. Fix what's above, then run make update again."
    exit 1
  fi

  if [ "$old" != "$new" ]; then
    say "3. Fast-forward main to $(short "$new")"
    if ! git merge --ff-only --quiet "$new"; then
      say "   failed: git merge --ff-only $(short "$new") (above). The checkout is still at $(short "$old")."
      say "Start netkeep serve again on the code you had"
      "$netkeep" update --checkout-start || true
      say ""
      say "make update stopped: the checkout didn't change. Fix what git said, then run"
      say "make update again."
      exit 1
    fi
  else
    say "3. Fast-forward main: nothing to do"
  fi

  say "4. Install with uv sync"
  uv sync --all-extras --frozen \
    || stopped_after_pull "uv sync --all-extras --frozen failed"

  say "5. Build the UI"
  (cd frontend && pnpm install --frozen-lockfile && pnpm build) \
    || stopped_after_pull "the UI build failed"

  say "6. Start netkeep serve"
  if ! "$netkeep" update --checkout-start; then
    say ""
    say "make update stopped: netkeep serve didn't start on the new code (above)."
    say "Where things stand:"
    say "  - The checkout is at $(short "$new"), it was at $(short "$old"), and .venv and"
    say "    the UI match it."
    say "  - The new code's serve may have migrated the database. Step 2 named the backup"
    say "    from before the update."
    say "What to run next:"
    say "  - Fix what's above, then run make update again."
    say "  - Or go back to $(short "$old"), forgetting what netkeep did since that backup:"
    say "      .venv/bin/netkeep login-item off"
    say "      git reset --keep $old"
    say "      uv sync --all-extras --frozen"
    say "      make build-ui"
    say "      .venv/bin/netkeep restore <the backup step 2 named>"
    say "      .venv/bin/netkeep login-item on"
    exit 1
  fi
  say "Done: the checkout is at $(short "$new")."
}

# Parsed whole before it runs: step 3 may replace this file.
main "$@"
