# Installation and update options

This document holds the research and the per-option comparison behind [ADR 0011](../adr/0011-installation-and-updates.md). The ADR holds the decision and its reasoning. This document holds what each option asks of a new user and of us, as researched on 2026-10-17 (#594). Facts about third-party tools drift; check them again when an implementation issue starts.

## Where netkeeper is today

- **Package.** `pyproject.toml` builds a pure-Python wheel with hatchling (`packages = ["netkeeper"]`), one console script (`netkeeper = "netkeeper.cli:app"`), and `requires-python = ">=3.12"`. The name `netkeeper` is free on PyPI. Nothing is published.
- **Dependencies.** Every dependency ships macOS wheels for Apple silicon and Intel, so an install from PyPI compiles nothing and needs no Xcode Command Line Tools. Playwright ships wheels only (no sdist), each with a bundled Node driver; netkeeper uses it attach-only, so `playwright install` never runs (ADR 0002).
- **Python.** `.python-version` pins 3.12 and `[tool.uv] python-preference = "only-managed"`, so uv uses its own CPython, never the host's.
- **UI.** `netkeeper/web/frontend.py` serves `frontend/dist` from the repository root (or `$NETKEEPER_FRONTEND_DIST`). Building it needs Node 22 and pnpm (`make build-ui`). A wheel today carries no UI. #596 moves the build into CI and into the wheel.
- **Run at login.** `scripts/install-launchd.sh` writes `~/Library/LaunchAgents/fun.tnkr.netkeeper.plist`, which runs `scripts/serve-launchd.sh` from the checkout's `.venv`. It assumes a git checkout. #600 moves it into a Settings switch.
- **Chrome.** netkeeper never starts a browser today (ADR 0002); `scripts/chrome.sh` starts the dedicated profile by hand. #595 decides how that changes.
- **Data and secrets.** The data directory defaults to `~/Library/Application Support/netkeeper`. Keychain entries use the service `netkeeper` for the default directory and `netkeeper@<hash>` for any other (#580). Neither depends on where the code is installed, so a change of install method keeps the data and the secrets, provided the data directory stays the same.

So a new user today needs git, uv, Node 22, pnpm, Chrome, and a terminal, and runs about eight commands (spec 16).

## The options

### A. uv tool from PyPI

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh    # only if uv is missing
uv tool install netkeeper
netkeeper setup
```

- **User needs:** macOS, Chrome, a terminal. uv installs to `~/.local/bin` with no admin password, and downloads its own managed CPython when none fits. No Xcode CLT, Homebrew, or Node.
- **Updates:** `uv tool upgrade netkeeper` keeps the original install's constraints and settings and reinstalls the executables. A `netkeeper update` wraps it with the steps netkeeper needs (below).
- **Uninstall:** `netkeeper setup --remove` (removes the login item), then `uv tool uninstall netkeeper`. The data directory and Keychain entries stay.
- **Cost to us:** a PyPI project, a release workflow with trusted publishing (GitHub OIDC, no stored token; `uv publish` supports it, and the PyPA publish action adds PEP 740 attestations), and a version scheme. No Apple account.
- **Gatekeeper:** none. Files that uv and curl write carry no quarantine attribute, so Gatekeeper never assesses them.
- **Risks:** the tool environment is tied to one interpreter; a reinstall that picks a newer CPython moves the binary. See "Keychain prompts" below. The executable directory must be on `PATH` (`uv tool update-shell` fixes it, but needs a new shell).

### B. One-line installer

```sh
curl -fsSL https://github.com/dsmorgan/netkeeper/releases/latest/download/install.sh | sh
```

A short POSIX script that does option A's three steps, then opens the first-run page. It is a front end to A, not a separate channel: everything it installs, A installs.

- **User needs:** the same as A, and one copy and paste.
- **Updates and uninstall:** A's.
- **Cost to us:** the script, its tests (the `install-launchd.sh` tests show the pattern: a fake `uname` and `launchctl`), and attaching it to each release.
- **Security tradeoffs:** piping a script to a shell runs code the user didn't read, with no signature check, and automated scanners flag the pattern. In practice the user already trusts the same host for the code itself, so the script adds little new trust, but it should earn what trust it asks for:
  - Wrap the body in a function called on the last line, so a download cut short runs nothing.
  - Serve it over HTTPS from the GitHub release, and pin the netkeeper version it installs to that release.
  - Install uv by its own installer only when `uv` is missing, and say so before doing it.
  - No `sudo`, nothing outside the user's home directory, and print each step.
  - Document the two-step alternative (download, read, run) next to the one-liner.

### C. Homebrew tap

```sh
brew install dsmorgan/netkeeper/netkeeper
netkeeper setup
```

- **User needs:** Homebrew, which itself needs the Xcode Command Line Tools and an admin password on first install. Many technical users have it; a non-technical user doesn't.
- **Updates:** `brew upgrade`, and Homebrew's automatic update on each `brew` command. No in-app updater: netkeeper must not upgrade itself under Homebrew.
- **Uninstall:** `brew uninstall netkeeper`. Data and Keychain stay.
- **Cost to us:** a `homebrew-netkeeper` repository with a formula using `Language::Python::Virtualenv`, a resource stanza per dependency (generated, for example with homebrew-pypi-poet, and regenerated each release), and CI that bumps the formula on release. Homebrew builds Python resources from sdists by convention; Playwright has none, so the formula has to install its wheel, which is acceptable in our own tap. homebrew-core would mean meeting its acceptance policy and its from-source norm, so it's out of reach for now.
- **Gatekeeper:** none for a formula; Homebrew's Gatekeeper rules (including its September 2026 end of support for casks that fail Gatekeeper) apply to casks only.
- **Risks:** a formula's paths move on every upgrade (`Cellar/netkeeper/<version>`), so the LaunchAgent must point at the stable `$(brew --prefix)/bin/netkeeper`. `brew services` would install a second login item (`homebrew.mxcl.netkeeper`) beside netkeeper's own (`fun.tnkr.netkeeper`); the formula should not declare a `service` block. Homebrew upgrades its own `python@3.12` independently, which moves the interpreter (see "Keychain prompts").

### D. Signed, notarized Mac app

A `.app` in a `.dmg`, built with Briefcase, py2app, or PyInstaller (one-directory mode; one-file mode unpacks to a temporary directory at each start, which fights signing), possibly with a menu-bar icon that starts and stops `serve`.

- **User needs:** nothing but macOS and Chrome. Drag to Applications, open, done. The most familiar flow for a non-technical user.
- **Updates:** an in-app updater (Sparkle is the usual choice; it needs its own signing key and an appcast feed), or "download the new DMG".
- **Uninstall:** drag to the Trash, plus a menu item that removes the login item first. Data and Keychain stay.
- **Cost to us:** the Apple Developer Program (US$99 a year), a Developer ID Application certificate, and notarization in CI (`notarytool`, the certificate and an API key as CI secrets). Every binary inside the bundle must be signed with the hardened runtime: the CPython interpreter, every extension module (`regex`, `uvloop`, `httptools`, `pydantic-core`, and so on), and Playwright's bundled Node driver, which likely needs a JIT entitlement. Either a universal2 build or one build per architecture. Each release is a CI build of 100 MB or more, signed and notarized, on a macOS runner.
- **Gatekeeper:** a notarized, stapled app opens normally. An unnotarized one is the real risk: since macOS 15 the Control-click "Open" override is gone, and the user must try to open the app, then go to System Settings, Privacy & Security, click **Open Anyway**, and enter an admin password. Asking a non-technical user to do that defeats the point of an app. macOS 26 keeps that flow as far as we found.
- **Risks:** the largest build and maintenance surface by far, for one maintainer; a signing or notarization break blocks a release. On the other side, a stable code signature gives Keychain items a stable owner across updates, the one problem the other options can't solve.

## Comparison

| | A. uv tool | B. One-liner (front end to A) | C. Homebrew tap | D. Signed .app |
|---|---|---|---|---|
| Steps for a new user | 2 to 3 commands | 1 paste | 2 commands, after Homebrew | Download, drag, open |
| Prerequisites | Terminal | Terminal | Homebrew (Xcode CLT, admin) | None |
| Updates | `netkeeper update` (uv) | Same as A | `brew upgrade` | In-app updater (Sparkle) |
| Gatekeeper | Not assessed | Not assessed | Not assessed (formula) | Must be notarized |
| Our cost | PyPI + release workflow | A + one tested script | A + a tap repo + formula bumps | A's build + $99/yr + signing, notarization, per-arch builds |
| Apple silicon and Intel | Both, from wheels | Both | Both | universal2 or two builds |
| Interpreter moves | On reinstall | On reinstall | On Homebrew's Python upgrade | Never (bundled) |
| Fits #596 prebuilt UI | Needs it | Needs it | Needs it | Needs it |
| Fits #600 login item | `netkeeper setup` | `netkeeper setup` | `netkeeper setup`, no `brew services` | The app owns it |

## Concerns that cut across every option

### The update step

`netkeeper update` (and the formula's or app's equivalent) has to do more than replace files:

1. Refuse, or hand off, while a manual `serve` holds the serve lock (#590).
2. Take a backup (`netkeeper backup`), because the new version's `serve` migrates the database at start.
3. Upgrade the package.
4. Restart the login item, if installed, so the new code runs; `serve` migrates as it starts, as `serve-launchd.sh` does today.

An in-UI check can show that a newer version exists: one request to PyPI's JSON API, at most daily, sending nothing but the request itself, with a setting to turn it off. It shows the command to run; it never installs on its own. Under Homebrew it says `brew upgrade`.

### Keychain prompts after an interpreter change

macOS ties a Keychain item's access list to the program that created it. netkeeper's process is the Python interpreter, and uv's and Homebrew's interpreters are at best ad-hoc signed, so a new interpreter binary is a different program to the Keychain. After an interpreter change, the first Gmail read asks "python3.12 wants to use your confidential information" once per item, and the user clicks **Always Allow**. pgAdmin's and keyring's issue trackers report the same behavior. It is a prompt, not data loss: the service name (#580) and the entries are unchanged. Today's checkout install has the same exposure. Options A to C should state it in the update notes, and the implementation should test whether `uv tool upgrade` keeps the interpreter (it keeps the environment unless reinstalled) and whether `serve` surfaces a denied Keychain read clearly. Only option D removes it.

### The data directory and #580

Every option must keep `~/Library/Application Support/netkeeper` as the default and never set `NETKEEPER_DATA` in the LaunchAgent unless the user chose another directory, so the Keychain service stays `netkeeper` and an existing install keeps its secrets. Moving from a checkout to an installed copy is then: remove the checkout's login item, install, run `netkeeper setup`.

### The LaunchAgent and #600

The plist must point at the installed, stable executable (`~/.local/bin/netkeeper`, or `$(brew --prefix)/bin/netkeeper`), not at a checkout's `.venv` and not at a versioned path. `netkeeper setup` and the #600 Settings switch should share one implementation, with the same label (`fun.tnkr.netkeeper`), and `scripts/install-launchd.sh` stays for a checkout.

### Chrome and #595

No installer installs or configures Chrome: netkeeper is Chrome only, and Chrome is a prerequisite the first-run page checks. Whatever #595 decides about the LinkedIn profile and the UI's window, setup leads to the first-run page and the first-run page walks through it. Nothing here changes ADR 0002's attach-only rule; a managed launch of netkeeper's own profile, if #595 adopts it, comes from that ADR.

## Sources

- uv tools: <https://docs.astral.sh/uv/concepts/tools/>
- uv Python installs and reinstalls: <https://docs.astral.sh/uv/guides/install-python/>, <https://simonwillison.net/2025/Jan/7/uv-python-reinstall/>
- PyPI trusted publishing with uv: <https://pydevtools.com/handbook/how-to/how-to-publish-to-pypi-with-trusted-publishing/>, <https://pydevtools.com/handbook/how-to/how-to-publish-python-packages-with-digital-attestations/>
- Homebrew Python formulae: <https://docs.brew.sh/Python-for-Formula-Authors>; acceptable formulae: <https://docs.brew.sh/Acceptable-Formulae>; automating a tap: <https://til.simonwillison.net/homebrew/auto-formulas-github-actions>
- Apple notarization: <https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution>
- macOS 15 Gatekeeper changes: <https://appleinsider.com/inside/macos-sequoia/tips/whats-changed-in-runtime-protection-for-macos-sequoia>, <https://mjtsai.com/blog/?p=43993>
- Keychain prompts after a Python change: <https://github.com/jaraco/keyring/issues/525>, <https://www.postgresql.org/message-id/CA%2BOCxoxp_ZK7w5z_ifOKv6rWmQ%3DGv4aXqirCWsgoaRYrxvrLAQ%40mail.gmail.com>
