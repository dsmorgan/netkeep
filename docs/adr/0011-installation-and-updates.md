# 0011. Installation and updates: PyPI and uv first, a one-line installer in front, Homebrew next, a signed app only if needed

Date: 2026-10-17

## Status

Accepted (#594, 2026-10-18), by the maintainer. The implementation is still open on #594.

## Context

Installing netkeeper today takes git, uv, Node 22, pnpm, Chrome, and about eight commands (spec 16). The maintainer wants installing to take one step, or as few as possible (#594).

Four channels were researched: a uv tool from PyPI, a one-line `curl | sh` installer, a Homebrew tap, and a signed Mac app. The research and sources are in [Installation and update options](../design/install-options.md). The deciding facts:

- netkeeper is a pure-Python wheel. On Apple silicon every dependency ships a wheel, so uv installs it with its own managed Python and no admin password, Xcode tools, Homebrew, or Node. On Intel, cryptography 49 and later ship no wheel.
- A tool install ignores `uv.lock` and resolves fresh, unless we pin it.
- Gatekeeper doesn't assess files that uv, curl, and Homebrew write. Since macOS 15, an unnotarized downloaded app opens only through System Settings and an admin password.
- A Mac app needs the US$99-a-year Apple Developer Program and notarization of every binary inside: the largest upkeep by far.
- Homebrew itself needs the Xcode Command Line Tools and an admin password.
- The data directory and the Keychain service (#580) don't depend on where the code lives.

## Decision

netkeeper becomes netkeep (#661), publishes to PyPI as `netkeep`, and installs as a uv tool. For 1.0, a one-line installer on each GitHub release installs uv if it's missing, installs netkeep, then runs `netkeep setup`, which installs the login item and opens the first-run page. `netkeep update` upgrades in place. A Homebrew tap follows. A signed Mac app waits for a new ADR, if users who won't open a terminal matter.

### Rules

1. **One tested dependency set per release.** Each release attaches a constraints file exported from `uv.lock`, and every install and update runs `uv tool install --python 3.12 --managed-python -c <constraints> netkeep==X.Y.Z`. Without the pin, users get dependencies CI never ran.
2. **Apple silicon is fully supported; Intel is supported while cryptography 48.x is acceptable,** through a complementary pair in `pyproject.toml` (`cryptography<49` on macOS x86_64, `>=49` elsewhere) and a CI job that installs the wheel on an Intel runner. A frozen major version gets no security fixes, so this has an end date.
3. **The UI ships inside the wheel (#596).** Every channel depends on it.
4. **Releases publish from CI with trusted publishing** from a protected environment, with no stored PyPI token. A leaked token is the cheapest way to ship malware under our name.
5. **The installer earns the trust it asks for.** It runs nothing unless it downloaded whole, pins its netkeeper release and its uv installer, never uses `sudo` or writes outside the home directory, and prints each step. Release assets are immutable and checksummed; the docs give a download-verify-run alternative.
6. **`netkeeper setup` and the #600 Settings switch are one implementation of one login item** (`fun.tnkr.netkeeper` until #661, then `fun.tnkr.netkeep`), pointing at the stable installed executable. The formula declares no `brew services` block. Two login items would race for the serve lock.
7. **Install and update never change the default data directory,** so the #580 Keychain service and secrets survive. The one exception is the #661 move.
8. **An update stops `serve`, backs up, and rolls back only when that's safe.** `netkeeper update` refuses while a manual `serve` runs, stops the login item and confirms the serve lock is free, backs up, and upgrades; then it re-executes the newly installed `netkeeper update --finish`, which starts the login item and runs a health check, so no step runs on replaced code. Each install keeps its constraints file in the data directory. If the upgrade or health check fails, it reinstalls the old pinned version, restores the pre-update backup with the login item off, starts it, and checks again. It does that automatically only if the new version failed before its scheduler ran; otherwise it stops and tells the user, because a restore would forget what the new version sent and risk a double send.
9. **netkeeper never updates itself silently.** The UI may check PyPI at most daily, with a setting to turn it off. It shows the command and installs nothing.
10. **No installer installs or configures Chrome.** How netkeeper's LinkedIn Chrome starts is [ADR 0012](0012-chrome-profiles.md)'s decision (#595, #599),, and ADR 0002 stands unless it changes.
11. **Uninstalling keeps data and secrets.** `netkeeper setup --remove`, then the package manager's uninstall. Removing a program shouldn't remove a network's history.

### Phases

- **1.0:** #596; the PyPI release workflow with the constraints file; the Intel CI job; `netkeeper setup` and `netkeeper update`; the installer and its tests; a README that leads with the one-liner.
- **After 1.0:** the update check; a `dsmorgan/homebrew-netkeeper` tap, bumped by CI.
- **Later, if needed:** a signed app with a menu-bar icon, by a new ADR.

### Maintainer answers

- **The PyPI name.** The PyPI `netkeeper` project isn't ours. The maintainer picked **netkeep** on #641: PyPI and GitHub had no `netkeep` project or repository on 2026-10-18. #661 is the one planned move. It renames the distribution, command, and repository; moves the data directory once, after a backup; copies the Keychain items; migrates the launchd label; and renames the `NETKEEPER_*` variables (old names work for one release) and the session cookie. It lands before the first PyPI release. After it, rules 6 and 7 apply to the `netkeep` names. Rules below use today's `netkeeper` names.
- **Intel is in 1.0.** Rule 2 and the Intel CI job apply to 1.0.

## Consequences

- A new user with Chrome pastes one line and reaches the first-run page with no Node, git, Homebrew, or admin password.
- We take on PyPI publishing, versioned releases, and a tested shell script, not an Apple account.
- The one-liner needs a terminal.
- After an interpreter change, macOS asks once per Keychain item to let the new interpreter read it: a prompt, not data loss.
