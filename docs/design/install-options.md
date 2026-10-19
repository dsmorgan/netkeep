# Installation and update options

This document holds the research and the per-option comparison behind [ADR 0011](../adr/0011-installation-and-updates.md). The ADR holds the decision and its reasoning. This document holds what each option asks of a new user and of us, as researched on 2026-10-17 (#594). Facts about third-party tools drift; check them again when an implementation issue starts.

## Where netkeep is today

- **Package.** `pyproject.toml` builds a pure-Python wheel with hatchling (`packages = ["netkeep"]`), one console script (`netkeep = "netkeep.cli:app"`), and `requires-python = ">=3.12"`. Nothing is published. The PyPI project `netkeep` is already registered, with no releases (see "The PyPI name" below).
- **Dependencies.** On Apple silicon every runtime dependency in `uv.lock` ships a wheel, so an install from PyPI compiles nothing and needs no Xcode Command Line Tools. On Intel, one doesn't: cryptography, which google-auth requires, dropped macOS x86_64 wheels in 49.0.0, and the locked 50.x ships `macosx_11_0_arm64` only (see "Intel Macs" below). Playwright ships wheels only (no sdist), one per architecture plus universal2, each with a bundled Node driver; netkeep uses it attach-only, so `playwright install` never runs (ADR 0002).
- **Python.** In a checkout, `.python-version` pins 3.12 and `[tool.uv] python-preference = "only-managed"`, so uv uses its own CPython. A tool install reads none of that, nor `uv.lock`: it resolves fresh from the wheel's metadata (`>=3.12`, floating lower bounds), and may pick any Python 3.12 or later it finds, a Homebrew one included. So the install command has to pin all three (option A).
- **UI.** `netkeep/web/frontend.py` serves `frontend/dist` from the repository root (or `$NETKEEP_FRONTEND_DIST`). Building it needs Node 22 and pnpm (`make build-ui`). A wheel today carries no UI. #596 moves the build into CI and into the wheel.
- **Run at login.** The Settings switch and `netkeep login-item on|off|status` (#600) write `~/Library/LaunchAgents/<label>.plist`, which runs the installed `netkeep serve`; `fun.tnkr.netkeep` is the default data directory's label. They replaced `scripts/install-launchd.sh` and `serve-launchd.sh`, which assumed a git checkout.
- **Chrome.** netkeep never starts a browser today (ADR 0002); `scripts/chrome.sh` starts the dedicated profile by hand. #595 decides how that changes.
- **Data and secrets.** The data directory defaults to `~/Library/Application Support/netkeep`. Keychain entries use the service `netkeep` for the default directory and `netkeep@<hash>` for any other (#580). Neither depends on where the code is installed, so a change of install method keeps the data and the secrets, provided the data directory stays the same.

So a new user today needs git, uv, Node 22, pnpm, Chrome, and a terminal, and runs about eight commands (spec 16).

## The options

### A. uv tool from PyPI

```sh
curl -LsSf https://astral.sh/uv/<uv version>/install.sh | sh    # only if uv is missing
curl -fsSLo constraints.txt https://github.com/dsmorgan/netkeeper/releases/download/vX.Y.Z/constraints.txt
uv tool install --python 3.12 --managed-python -c constraints.txt netkeep==X.Y.Z
netkeep setup
```

The constraints file is `uv export --no-dev --no-hashes` from the release's `uv.lock`, attached to the same GitHub release, so the dependency set a user gets is the one CI tested. Each install also copies it into the data directory (for example `<data_dir>/install/constraints-X.Y.Z.txt`, beside a record of the installed version), so a rollback can reinstall the old version without the network. The export leaves out hashes because `uv tool install -c` doesn't check them: on 2026-10-17, uv 0.12.23 installed a package whose constraint carried a wrong `--hash`, with `UV_REQUIRE_HASHES` and `UV_VERIFY_HASHES` set too, and `uv tool install --help` lists no hash option. What protects a pinned version is PyPI itself: a file once uploaded can't be replaced under the same name. A new file added to an old release (another wheel tag) is the gap the `==` pin doesn't close; re-test hash support when uv adds it, and export with hashes then. `--python 3.12 --managed-python` keeps the interpreter to uv's own 3.12, the one CI runs. Nobody types this by hand: the installer (option B) and `netkeep update` run it.

- **User needs:** macOS, Chrome, a terminal. uv installs to `~/.local/bin` with no admin password, and downloads its own managed CPython when none fits. No Xcode CLT, Homebrew, or Node.
- **Updates:** `uv tool upgrade` keeps the original install's constraints, so it can't move to a new release's constraints file. `netkeep update` runs the pinned `uv tool install` above for the new version instead, inside the steps netkeep needs (below).
- **Uninstall:** `netkeep setup --remove` (removes the login item), then `uv tool uninstall netkeep`. The data directory and Keychain entries stay.
- **Cost to us:** a PyPI project (see "The PyPI name"), a release workflow that attaches the constraints file, an Intel install job, trusted publishing (GitHub OIDC, no stored token; `uv publish` supports it, and the PyPA publish action adds PEP 740 attestations), and a version scheme. No Apple account.
- **Gatekeeper:** none. Files that uv and curl write carry no quarantine attribute, so Gatekeeper never assesses them.
- **Risks:** the tool environment is tied to one interpreter; an update that picks a newer 3.12 patch moves the binary. See "Keychain prompts" below. The executable directory must be on `PATH` (`uv tool update-shell` fixes it, but needs a new shell).

### B. One-line installer

```sh
curl -fsSL https://github.com/dsmorgan/netkeeper/releases/latest/download/install.sh | sh
```

A short POSIX script that does option A's three steps, then opens the first-run page. It is a front end to A, not a separate channel: everything it installs, A installs.

- **User needs:** the same as A, and one copy and paste.
- **Updates and uninstall:** A's.
- **Cost to us:** the script, its tests (the login item's tests show the pattern: a fake `launchctl` runner and an injected agents directory), and attaching it to each release.
- **Security tradeoffs:** piping a script to a shell runs code the user didn't read, with no signature check, and automated scanners flag the pattern. In practice the user already trusts the same host for the code itself, so the script adds little new trust, but it should earn what trust it asks for:
  - Wrap the body in a function called on the last line, so a download cut short runs nothing.
  - Serve it over HTTPS from the GitHub release, with GitHub's immutable releases turned on, so nobody with write access can swap `install.sh` or the constraints file after publication. `releases/latest/download/` moves with each release, so the docs also show the versioned URL (`releases/download/vX.Y.Z/install.sh`).
  - Pin the netkeep version with `==` (`netkeep==X.Y.Z`) plus that release's constraints file, so a compromised later PyPI upload can't ride an old installer.
  - Install uv only when `uv` is missing, and say so first. Pin uv too: a versioned installer URL (`https://astral.sh/uv/<version>/install.sh`), or uv's release tarball checked against its published sha256. An unpinned `astral.sh/uv/install.sh` is a second, unpinned `curl | sh` from a second party.
  - Protect the publishing path: 2FA on the PyPI and GitHub accounts, and trusted publishing from a protected GitHub environment with a required reviewer and tag protection, so a stolen GitHub session alone can't publish.
  - Publish a checksum (or a Sigstore attestation) for `install.sh` in the release notes, so the read-then-run path has something to verify against.
  - No `sudo`, nothing outside the user's home directory, and print each step.
  - Document the alternative (download, verify, read, run) next to the one-liner.

### C. Homebrew tap

```sh
brew install dsmorgan/netkeeper/netkeep
netkeep setup
```

- **User needs:** Homebrew, which itself needs the Xcode Command Line Tools and an admin password on first install. Many technical users have it; a non-technical user doesn't.
- **Updates:** `brew upgrade`, and Homebrew's automatic update on each `brew` command. No in-app updater: netkeep must not upgrade itself under Homebrew.
- **Uninstall:** `brew uninstall netkeep`. Data and Keychain stay.
- **Cost to us:** a `homebrew-netkeep` repository with a formula using `Language::Python::Virtualenv`, a resource stanza per dependency (generated and refreshed with `brew update-python-resources`; homebrew-pypi-poet is unmaintained), and CI that bumps the formula on release. Homebrew builds Python resources from sdists by convention; Playwright has none, so the formula has to install its wheel, with `on_arm` and `on_intel` resources (or the universal2 wheel), which is acceptable in our own tap. The cryptography constraint for Intel (below) applies here too. homebrew-core would mean meeting its acceptance policy and its from-source norm, so it's out of reach for now.
- **Gatekeeper:** none for a formula; Homebrew's Gatekeeper rules (including its September 2026 end of support for casks that fail Gatekeeper) apply to casks only.
- **Risks:** a formula's paths move on every upgrade (`Cellar/netkeep/<version>`), so the LaunchAgent must point at the stable `$(brew --prefix)/bin/netkeep`. `brew services` would install a second login item (`homebrew.mxcl.netkeep`) beside netkeep's own (`fun.tnkr.netkeep`); the formula should not declare a `service` block. Homebrew upgrades its own `python@3.12` independently, which moves the interpreter (see "Keychain prompts").

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
| Updates | `netkeep update` (uv) | Same as A | `brew upgrade` | In-app updater (Sparkle) |
| Gatekeeper | Not assessed | Not assessed | Not assessed (formula) | Must be notarized |
| Our cost | PyPI + release workflow + constraints file + Intel CI job | A + one tested script | A + a tap repo + formula bumps | A's build + $99/yr + signing, notarization, per-arch builds |
| Apple silicon and Intel | Apple silicon from wheels; Intel only with the `cryptography` marker pair | Same as A | Same as A | universal2 or two builds |
| Interpreter moves | On a 3.12 patch change at update | Same as A | On Homebrew's Python upgrade | Never (bundled) |
| Fits #596 prebuilt UI | Needs it | Needs it | Needs it | Needs it |
| Fits #600 login item | `netkeep setup` | `netkeep setup` | `netkeep setup`, no `brew services` | The app owns it |

## Concerns that cut across every option

### The update step

`netkeep update` (and the formula's or app's equivalent) has to do more than replace files:

1. If a manual `serve` holds the serve lock (#590), refuse with a message that says to stop it. Never signal a process the user owns in a terminal.
2. Otherwise stop the login item (`launchctl bootout`) and confirm the serve lock is free. The plist's `KeepAlive` relaunches a crashed `serve`, so an upgrade under a running job could start a half-installed copy, and that copy would migrate the database. Replacing files under a running process can also break its lazy imports.
3. Take a backup while nothing has the database open, because the new version's `serve` migrates at start.
4. Upgrade: the pinned `uv tool install` of the new version (option A).
5. Re-execute the newly installed `netkeep update --finish` (a hidden subcommand), which starts the login item again (`launchctl bootstrap`); `serve` migrates as it starts, as the login item's agent does today (it runs `netkeep serve` directly).
6. Health check, in `--finish`: the serve lock is held, the database revision equals the new version's head, and `/health` answers, within a time limit.

**Replacing its own code.** A successful `uv tool install` syncs the same environment in place, so after step 4 the running `update` process sits on files that may be the new version's or a mix. So the old process does nothing after step 4 but run `uv` and `exec` a fresh binary: the new `netkeep update --finish` on success, or, on failure, the rollback's reinstall and then the old version's `netkeep update --rollback` (also hidden). A health check that fails in `--finish` does the same: reinstall the old version, then `exec` its `--rollback`, which runs rollback steps 2 to 4 on code that matches what's installed. Steps 1 to 4 import everything they need before step 4 starts. The alternative, a small script copied out of the environment that runs steps 4 to 6, is equivalent but harder to test; re-executing is the choice.

**Rollback.** When step 4 or step 6 fails:

1. Reinstall the old pinned version: `uv tool install` at the old `==` version with the constraints file the data directory kept for it.
2. With the login item still off, `netkeep restore` the pre-update backup. The old version can't start against a database the new one migrated: its `serve` stops on a revision it doesn't know, and `KeepAlive` restarts it in a loop. There's no `db downgrade`, and some migrations' `downgrade()` is empty, so the backup is the only way back. `restore` takes the serve lock and accepts a revision the old migrations know.
3. Start the login item.
4. Run the health check again, against the old head.

**When the rollback may run on its own.** Only when the new version failed before its scheduler ran: step 4 failed, the migration failed, or the health check failed at startup. The backup was taken with `serve` stopped, so nothing is lost. If the new version's scheduler ran (it failed later, or the health check passed and the user asks for a rollback afterward), `update` stops, leaves the login item off, and tells the user what happened and what to run. A restore then would forget messages the new version sent, and the old version could send them again. `serve` has to record that its scheduler started for this to be decidable; the implementation issue adds that marker.

An in-UI check can show that a newer version exists: one request to PyPI's JSON API, at most daily, sending nothing but the request itself, with a setting to turn it off. It shows the command to run; it never installs on its own. Under Homebrew it says `brew upgrade`.

### Intel Macs

cryptography 49.0.0 removed macOS x86_64 support, and 48.0.1 is the last release with Intel wheels. On an Intel Mac a fresh resolve picks 50.x, finds only an sdist, and tries to build it, which needs Rust and the Command Line Tools: a stock Mac fails. The choices:

- **Constrain cryptography on Intel.** A complementary pair in `pyproject.toml`'s dependencies:

  ```toml
  "cryptography<49; sys_platform == 'darwin' and platform_machine == 'x86_64'",
  "cryptography>=49; sys_platform != 'darwin' or platform_machine != 'x86_64'",
  ```

  `uv lock` then forks, and `uv export` emits one pin per side (48.0.1 for macOS x86_64, 50.x for the rest), each with its marker. The pair also protects an install that skips the constraints file. Two placements that look simpler don't work (re-review of #615, uv 0.12.23): the marker appended to the exported constraints is unsatisfiable on Intel, because the export already pins `cryptography==50.0.1` with no marker; and the single marker in `pyproject.toml` doesn't fork the lock, so every platform drops to 48.x. Intel keeps working with no toolchain. The cost: a frozen major version gets no security fixes, and a future google-auth that needs cryptography 49 or later ends it.
- **`--no-build`.** The resolver then picks only versions that have a wheel for the machine, which lands on the same 48.x for Intel without naming it. It's less explicit, and it can fail to resolve where the constraint would have said why.
- **Apple silicon only.** The simplest, and Intel Macs are a shrinking share; but it drops users we may have.

The ADR recommends the first, with a CI job that installs the built wheel on a macOS x86_64 runner, for as long as 48.x is acceptable. The maintainer decided that Intel is in 1.0 (2026-10-18).

### The PyPI name

Resolved: the project is renamed to `netkeep` (#661), so none of the fallbacks below is needed. The text below records the research.

`https://pypi.org/simple/netkeep/` (JSON) answers `"status": "active"` with no versions or files: the name is registered by someone, with no releases. `/pypi/netkeep/json` answers 404 only because there are no releases. Unless the maintainer owns it, a first upload gets 403. The fallbacks:

- **A PEP 541 request** for the empty project. It may succeed, but it takes weeks to months and isn't certain.
- **Another distribution name**, such as `netkeep-app`. `[project.scripts]` still installs the `netkeep` command, so only the install line in the README, the installer, and `netkeep update` change.

Either way, the name has to be settled before the release workflow, because the user types it.

### Keychain prompts after an interpreter change

macOS ties a Keychain item's access list to the program that created it. netkeep's process is the Python interpreter, and uv's and Homebrew's interpreters are at best ad-hoc signed, so a new interpreter binary is a different program to the Keychain. After an interpreter change, the first Gmail read asks "python3.12 wants to use your confidential information" once per item, and the user clicks **Always Allow**. pgAdmin's and keyring's issue trackers report the same behavior. It is a prompt, not data loss: the service name (#580) and the entries are unchanged. Today's checkout install has the same exposure. Options A to C should state it in the update notes, and the implementation should test whether `uv tool upgrade` keeps the interpreter (it keeps the environment unless reinstalled) and whether `serve` surfaces a denied Keychain read clearly. Only option D removes it.

### The data directory and #580

Every option must keep `~/Library/Application Support/netkeep` as the default and never set `NETKEEP_DATA` in the LaunchAgent unless the user chose another directory, so the Keychain service stays `netkeep` and an existing install keeps its secrets. Moving from a checkout to an installed copy is then: remove the checkout's login item, install, run `netkeep setup`.

### The LaunchAgent and #600

The plist must point at the installed, stable executable (`~/.local/bin/netkeep`, or `$(brew --prefix)/bin/netkeep`), not at a checkout's `.venv` and not at a versioned path. `netkeep setup` and the #600 Settings switch should share one implementation, with the same label (`fun.tnkr.netkeep`, plus a data-directory hash for any other data directory). `scripts/install-launchd.sh` was removed in #600: a checkout runs `netkeep login-item on` from its `.venv`.

### Chrome, #595 and #599

No installer installs or configures Chrome: netkeep is Chrome only, and Chrome is a prerequisite the first-run page checks. `scripts/chrome.sh` isn't in the wheel, and the CLI only prints the launch command, so an installed copy reaches the first-run page but can't start the LinkedIn Chrome on its own until #595 decides how it starts and #599 gives it its port. Until then the first-run page shows the printed command. Whatever #595 decides, setup leads to the first-run page and the first-run page walks through it. Nothing here changes ADR 0002's attach-only rule; a managed launch of netkeep's own profile, if #595 adopts it, comes from that ADR.

## Sources

- uv tools: <https://docs.astral.sh/uv/concepts/tools/>
- uv Python installs and reinstalls: <https://docs.astral.sh/uv/guides/install-python/>, <https://simonwillison.net/2025/Jan/7/uv-python-reinstall/>
- PyPI trusted publishing with uv: <https://pydevtools.com/handbook/how-to/how-to-publish-to-pypi-with-trusted-publishing/>, <https://pydevtools.com/handbook/how-to/how-to-publish-python-packages-with-digital-attestations/>
- cryptography 49 dropping macOS x86_64: <https://cryptography.io/en/latest/changelog/>
- PEP 541 (package index name retention): <https://peps.python.org/pep-0541/>
- Homebrew Python formulae: <https://docs.brew.sh/Python-for-Formula-Authors>; acceptable formulae: <https://docs.brew.sh/Acceptable-Formulae>; automating a tap: <https://til.simonwillison.net/homebrew/auto-formulas-github-actions>
- Apple notarization: <https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution>
- macOS 15 Gatekeeper changes: <https://appleinsider.com/inside/macos-sequoia/tips/whats-changed-in-runtime-protection-for-macos-sequoia>, <https://mjtsai.com/blog/?p=43993>
- Keychain prompts after a Python change: <https://github.com/jaraco/keyring/issues/525>, <https://www.postgresql.org/message-id/CA%2BOCxoxp_ZK7w5z_ifOKv6rWmQ%3DGv4aXqirCWsgoaRYrxvrLAQ%40mail.gmail.com>
