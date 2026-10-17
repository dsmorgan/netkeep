# 0011. Installation and updates: PyPI and uv first, a one-line installer in front, Homebrew next, a signed app only if needed

Date: 2026-10-17

## Status

Proposed (#594). The maintainer decides, and answers the open questions below.

## Context

Installing netkeeper today takes git, uv, Node 22, pnpm, Chrome, a terminal, and about eight commands from a checkout (spec 16). The run-at-login script assumes that checkout's `.venv`. The maintainer wants installing to need no expertise: one step, or as few as possible (#594).

Four channels were researched: a uv tool from PyPI, a one-line `curl | sh` installer, a Homebrew tap, and a signed Mac app. The research, the per-option table, and the sources are in [Installation and update options](../design/install-options.md). The facts that decide it:

- netkeeper is a pure-Python wheel. On Apple silicon every dependency ships a wheel, so uv installs it with its own managed Python and no admin password, Xcode tools, Homebrew, or Node. On Intel, cryptography 49 and later ship no wheel.
- A tool install ignores `uv.lock` and resolves fresh, unless we pin it.
- Gatekeeper doesn't assess files that uv, curl, and Homebrew formulae write. Since macOS 15, an unnotarized downloaded app opens only through System Settings and an admin password.
- A Mac app needs the US$99-a-year Apple Developer Program and every binary inside it signed and notarized: by far the largest upkeep for one maintainer.
- Homebrew itself needs the Xcode Command Line Tools and an admin password.
- The data directory and the Keychain service (#580) don't depend on where the code lives.

## Decision

netkeeper publishes to PyPI and installs as a uv tool. For 1.0, a one-line installer on each GitHub release installs uv if it's missing, installs netkeeper, then runs `netkeeper setup`, which installs the login item and opens the first-run page. `netkeeper update` upgrades in place. A Homebrew tap follows once releases are steady. A signed Mac app waits for a new ADR, if users who won't open a terminal become a goal.

### Rules

1. **One tested dependency set per release.** Each release attaches a constraints file exported from `uv.lock`, and every install and update runs `uv tool install --python 3.12 --managed-python -c <constraints> netkeeper==X.Y.Z`. The installer and the formula install that same release. Without the pin, users get dependencies and interpreters CI never ran.
2. **Apple silicon is fully supported; Intel is supported while cryptography 48.x is acceptable,** through a `cryptography<49` constraint for macOS x86_64 only and a CI job that installs the wheel on an Intel runner. A frozen major version stops getting security fixes, so this support has an end date.
3. **The UI ships inside the wheel (#596).** Every channel depends on it.
4. **Releases publish from CI with trusted publishing** from a protected environment, with no stored PyPI token. A leaked token is the cheapest way to ship malware under our name.
5. **The installer earns the trust it asks for.** It runs nothing unless it downloaded whole, pins its netkeeper release and its uv installer, never uses `sudo` or writes outside the home directory, and prints each step. Release assets are immutable and checksummed, and the docs give a download-verify-run alternative. `curl | sh` runs unread code; these limits keep it to what installing netkeeper already trusts.
6. **`netkeeper setup` and the #600 Settings switch are one implementation of one login item** (`fun.tnkr.netkeeper`), pointing at the stable installed executable. The formula declares no `brew services` block. Two login items would race for one serve lock.
7. **Install and update never change the default data directory,** so the #580 Keychain service and secrets survive.
8. **An update stops `serve` and backs up first.** `netkeeper update` refuses while a manual `serve` runs, stops the login item and confirms the serve lock is free, backs up, upgrades, and starts the login item again, which migrates as it starts. If the upgrade fails, it keeps the backup, reinstalls the previous pinned version, and starts that. Replacing code under a running `serve`, or migrating without a backup, are the ways an update loses data.
9. **netkeeper never updates itself silently.** The UI may check PyPI at most daily, with a setting to turn it off. It shows the command and installs nothing.
10. **No installer installs or configures Chrome.** How netkeeper's LinkedIn Chrome starts is #595's decision, with #599, and ADR 0002 stands unless that ADR changes it.
11. **Uninstalling keeps data and secrets.** `netkeeper setup --remove`, then the package manager's uninstall. Removing a program shouldn't remove a network's history.

### Phases

- **1.0:** #596; the PyPI release workflow with the constraints file; the Intel CI job; `netkeeper setup` and `netkeeper update`; the installer and its tests; a README that leads with the one-liner. The checkout install stays for development.
- **After 1.0:** the update check; a `dsmorgan/homebrew-netkeeper` tap, bumped by CI.
- **Later, if needed:** a signed app with a menu-bar icon, by a new ADR.

### Open questions for the maintainer

- **Is the PyPI `netkeeper` project yours?** It's registered with no releases. If it isn't, the fallback is a PEP 541 request or another distribution name, such as `netkeeper-app`, that still installs the `netkeeper` command.
- **Is Intel in 1.0?**

## Consequences

- A new user with Chrome pastes one line and reaches the first-run page with no Node, git, Homebrew, or admin password. Using LinkedIn from there needs #595 and #599.
- We take on PyPI publishing, versioned releases, and a tested shell script. No Apple account until we choose the app.
- The one-liner still needs a terminal. The deferred app would close that gap.
- After an interpreter change, macOS asks once per Keychain item to let the new interpreter read it. It's a prompt, not data loss; only a signed app removes it.
- Moving the maintainer's install off the checkout: remove its login item, install, run `netkeeper setup`.
