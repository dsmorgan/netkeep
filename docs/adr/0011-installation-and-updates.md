# 0011. Installation and updates: PyPI and uv first, a one-line installer in front, Homebrew next, a signed app only if needed

Date: 2026-10-17

## Status

Proposed (#594). The maintainer decides.

## Context

Installing netkeeper today takes git, uv, Node 22, pnpm, Chrome, a terminal, and about eight commands from a checkout (spec 16). The run-at-login script assumes that checkout's `.venv`. The maintainer wants installing to need no expertise: one step, or as few as possible, to be installed and running (#594).

Four channels were researched: a uv tool from PyPI, a one-line `curl | sh` installer, a Homebrew tap, and a signed Mac app. The research, the per-option table, and the sources are in [Installation and update options](../design/install-options.md). The facts that decide it:

- netkeeper is a pure-Python wheel whose every dependency ships macOS wheels for Apple silicon and Intel. uv installs it, with its own managed Python, with no admin password, Xcode tools, Homebrew, or Node.
- Files that uv, curl, and Homebrew formulae write aren't assessed by Gatekeeper. A downloaded app is, and since macOS 15 an unnotarized one can be opened only through System Settings and an admin password.
- A Mac app needs the US$99-a-year Apple Developer Program and every binary inside it signed and notarized, Playwright's bundled Node driver included: by far the largest upkeep for one maintainer.
- Homebrew needs the Xcode Command Line Tools and an admin password to install itself, so it isn't one step for someone who doesn't have it.
- The data directory and the Keychain service (#580) don't depend on where the code lives, so any channel keeps an existing install's data and secrets if it keeps the default data directory.

## Decision

netkeeper publishes to PyPI and installs as a uv tool. For 1.0, a one-line installer attached to each GitHub release installs uv if it's missing, runs `uv tool install netkeeper`, then `netkeeper setup`, which installs the login item and opens the first-run page. `netkeeper update` upgrades in place. A Homebrew tap follows once releases are steady. A signed, notarized Mac app is deferred until users who won't open a terminal are a goal; it would come as a new ADR.

### Rules

1. **One package, several front ends.** The installer and the Homebrew formula install the same PyPI release; neither carries code of its own. One build to test.
2. **The UI ships inside the wheel (#596), and the release needs no Node on the user's Mac.** Every channel depends on it, so it lands first.
3. **Releases publish from CI with trusted publishing,** with no stored PyPI token. A leaked token is the cheapest way to ship malware under our name.
4. **The installer earns the trust it asks for.** It runs nothing unless it downloaded whole, pins the release it came from, never uses `sudo` or writes outside the home directory, says before it installs uv, and prints each step. The docs give a download-read-run alternative next to the one-liner. `curl | sh` runs unread code; these limits keep it to what installing netkeeper already trusts.
5. **`netkeeper setup` and the #600 Settings switch are one implementation of one login item** (`fun.tnkr.netkeeper`), pointing at the installed, stable `netkeeper` executable, never at a versioned or checkout path. The formula declares no `brew services` block. Two login items would race for one serve lock.
6. **The default data directory is never changed by an install or an update.** The LaunchAgent sets `NETKEEPER_DATA` only when the user chose another directory, so the Keychain service stays the one #580 picks and secrets survive.
7. **An update backs up first.** `netkeeper update` hands off from a manual `serve` (#590), takes a backup, upgrades, and restarts the login item, because the new `serve` migrates the database as it starts. A migration is the one step of an update that can lose data.
8. **netkeeper never updates itself silently.** The UI may check PyPI for a newer version, at most daily, and with a setting to turn it off; it shows the command, and installs nothing. Under Homebrew it shows `brew upgrade`. A package manager owns its own files.
9. **No installer installs or configures Chrome.** Chrome is a prerequisite the first-run page checks. How netkeeper's LinkedIn Chrome starts is #595's decision, and ADR 0002's attach-only rules stand unless that ADR changes them.
10. **Uninstalling keeps data and secrets.** `netkeeper setup --remove`, then the package manager's uninstall; the data directory and Keychain entries stay unless the user deletes them on purpose. Removing a program shouldn't remove a network's history.

### Phases

- **1.0:** #596 (UI in the wheel); a PyPI release workflow; `netkeeper setup` and `netkeeper update`, sharing the #600 login-item code; the installer and its tests; a README that leads with the one-liner. The checkout install and `scripts/install-launchd.sh` stay for development.
- **After 1.0:** the update check in the UI; a `dsmorgan/homebrew-netkeeper` tap with a formula bumped by CI on each release.
- **Later, if needed:** a signed, notarized app with a menu-bar icon and an in-app updater, by a new ADR, when the maintainer wants users who won't use a terminal.

## Consequences

- A new user with Chrome pastes one line and lands on the first-run page. No Node, git, Homebrew, or admin password.
- We take on PyPI publishing, versioned releases, and a tested shell script. No Apple account or signing until we choose the app.
- The one-liner still asks for a terminal, which some users won't open. That's the gap the deferred app would close.
- After an interpreter change (a reinstall with a newer Python, or Homebrew's own Python upgrade), macOS asks once per Keychain item to let the new interpreter read it. It's a prompt, not data loss, and today's checkout has it too; only a signed app removes it.
- Moving the maintainer's install off the checkout: remove its login item, install, run `netkeeper setup`.
- Implementation issues follow acceptance.
