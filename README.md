# netkeep

[![CI](https://github.com/dsmorgan/netkeep/actions/workflows/ci.yml/badge.svg)](https://github.com/dsmorgan/netkeep/actions/workflows/ci.yml)

Keep your professional network warm.

netkeep pulls your 1st-degree LinkedIn connections and their contact info into a local database, helps you sort out who you actually know, and runs reconnect-and-follow-up sequences over Gmail and LinkedIn messaging. It replaces the manual workflow of exporting from LinkedIn, sorting a spreadsheet, scraping profiles with a separate tool, and mailing through a bulk-mail tool, with one application that runs on your Mac.

**Status: working toward 1.0.** Import, the CRM and triage, the LinkedIn extractor, email campaigns over Gmail, and LinkedIn messaging all work. Phase 6 (polish, backups, and these docs) ends with the 1.0 release. The [changelog](CHANGELOG.md) lists what has changed.

## Installing

On macOS 14 or later, paste this into Terminal:

```sh
curl -fsSL https://github.com/dsmorgan/netkeep/releases/latest/download/install.sh | sh
```

The installer prints each step before it runs it:

1. If you don't have [uv](https://docs.astral.sh/uv/), it installs it into `~/.local/bin`, using uv's own installer at a pinned version, checked against a pinned SHA-256.
2. It downloads the release's `constraints.txt` (the dependency versions CI tested) and `SHA256SUMS` over HTTPS, and stops unless the checksums match.
3. It runs `uv tool install --python 3.12 --managed-python -c constraints.txt netkeep==X.Y.Z`, for the release the installer came from. uv downloads its own Python 3.12 if it needs to.
4. It runs `netkeep setup`, which turns on the login item and opens the first-run page. A release without `netkeep setup` skips this step and tells you to run `netkeep serve`.

It never uses `sudo` and never writes outside your home directory, and it runs nothing unless the whole script downloaded. To see the steps without running them, add `--dry-run`: `curl -fsSL <url> | sh -s -- --dry-run`. The first release isn't out yet: until it is, install from a checkout (below).

### Download, verify, read, then run

To read the installer before it runs, download it from a specific release, with that release's checksums. Replace `vX.Y.Z` with the release you want from the [Releases](https://github.com/dsmorgan/netkeep/releases) page:

```sh
curl -fsSLO https://github.com/dsmorgan/netkeep/releases/download/vX.Y.Z/install.sh
curl -fsSLO https://github.com/dsmorgan/netkeep/releases/download/vX.Y.Z/SHA256SUMS
grep ' install.sh$' SHA256SUMS | shasum -a 256 -c -   # prints "install.sh: OK"
less install.sh                                       # read it
sh install.sh --dry-run                               # the steps it would run
sh install.sh
```

The checksums are also in the release notes. Releases are immutable: once published, a release's files can't be replaced.

### Requirements

- macOS 14 or later. The installer supports macOS only; on Linux, run netkeep from a checkout.
- Google Chrome. The UI requires it: other browsers may work, but they aren't tested or supported, and the UI shows a dismissible banner when you open it elsewhere. The LinkedIn steps drive their own, separate Chrome window, with its own profile (any Chromium build works there); Firefox and Safari have no DevTools protocol and can't be used for them.
- A Gmail account and your own Google Cloud OAuth client for the email steps. [docs/gmail-setup.md](docs/gmail-setup.md) walks through the setup.
- Optional, after 1.0: an Anthropic API key for the LLM features, which are deferred until after the 1.0 release. They will stay off unless you configure one.

An installed netkeep needs no Xcode tools, Homebrew, Node or git. To remove it, turn off the login item (`netkeep setup --remove`), then run `uv tool uninstall netkeep`. Your data directory and Keychain items stay.

### From a checkout, for development

A checkout also needs the Xcode command-line tools, for `git` (`xcode-select --install`), [uv](https://docs.astral.sh/uv/), and Node.js 22.12 or later with pnpm 10, to build the UI. With Node installed, `corepack enable` gives you the pnpm version the project pins.

```sh
git clone https://github.com/dsmorgan/netkeep.git
cd netkeep
make install                # create .venv and install netkeep
make build-ui               # build the UI into frontend/dist
.venv/bin/netkeep serve     # http://127.0.0.1:8000
```

In a checkout, run netkeep as `.venv/bin/netkeep`. The wheel and sdist that CI builds ship the UI inside the package, so an install from one needs no Node or pnpm. Without `make build-ui` in a checkout, the page says "The frontend is not built".

`make install` also installs the developer tools (tests, linters). You don't need them to use netkeep; `uv sync` installs netkeep alone. After a `git pull`, run `make install` and `make build-ui` again, then restart `serve`.

## What it does

netkeep is three tools in one, and they only make sense together:

1. **Extract.** A browser sidecar attaches to the Chrome you already use and reads your connections and their contact info with human-like pacing and hard daily budgets. It never runs a second browser identity.
2. **Organize.** A local CRM with a triage screen for "have I actually met this person", manual and rule-based tags, static and smart lists, CSV import with column mapping, and export presets. LinkedIn stays the source of truth through periodic re-sync.
3. **Reach out.** Multi-step sequences: an email, a follow-up a week later into the same thread, and a LinkedIn message after that, each skipped automatically when the person replies. Draft mode writes into Gmail for you to send by hand; send mode sends for you from the start you schedule, inside a daily cap.

Everything stays on your machine: one SQLite file, tokens in the macOS Keychain, a web UI on `127.0.0.1`. v1 is for one person, but the schema carries a user boundary from the start so a self-hosted or hosted multi-user deployment can follow later without a rewrite.

## Where the method comes from

The workflow is a five-stage reconnect method: validate your network, enrich it, reconnect in batches of about 100, handle responses, follow up a week later, and keep going weekly. It is described generically in [docs/networking-workflow.md](docs/networking-workflow.md), and [the spec](docs/architecture.md#3-mapping-to-the-reference-workflow) maps every manual step to a netkeep feature. The method comes from [hellophello](https://hellophello.com)'s job-search networking program, which is acknowledged here as the inspiration and appears nowhere in the implementation.

## Running

`netkeep serve` runs netkeep at <http://127.0.0.1:8000>; open the address in Google Chrome. `serve` creates the database the first time and migrates it after every update, so you don't need to run `netkeep db upgrade` yourself. The [setup guide](docs/guide/setup.md) takes you from install to your first import and sync.

### Upgrading from netkeeper

netkeep was called netkeeper. The first time the new version starts, it moves your data directory, copies your Keychain items, and replaces your login item under the new name. Back up first, run `netkeep migrate --dry-run` to see exactly what it will do, then start it. [Upgrading from netkeeper](docs/guide/upgrading-from-netkeeper.md) has the steps, and how to undo them.

### The browser sidecar

The LinkedIn steps attach to netkeep's own Chrome, with its own profile and a
debug port. A run never launches a browser. You start that Chrome yourself: with
**Start Chrome** in the UI, `netkeep open`, or the printed command, or, with the
setting **Start netkeep's Chrome when netkeep starts** on (off by default), when
`serve` starts. netkeep never starts or attaches to a Chrome on any other profile
([ADR 0012](docs/adr/0012-chrome-profiles.md)), and you sign in to LinkedIn yourself.

```sh
.venv/bin/netkeep open             # start netkeep's Chrome with the UI in its own window
.venv/bin/netkeep browser launch   # prints the Chrome command to run yourself
.venv/bin/netkeep preflight        # attach, session state, browser fingerprint
```

Log in to LinkedIn once in that window and use it for your own LinkedIn browsing too,
so your activity and netkeep's share one session. `preflight` reads the profile's
cookie names and a few `navigator` properties on a blank tab; it visits no website.

### Run at login (macOS)

Settings, **Run at login**, has a switch, "Start netkeep when I log in". It installs a user LaunchAgent that starts `netkeep serve` when you log in and restarts it if it crashes, and it shows the state it reads from the plist and `launchctl`. The command line does the same:

```sh
netkeep login-item on                  # install or replace the agent (--host and --port pass through to serve)
netkeep login-item status              # which state? starts at login? loaded in launchd? which plist and log?
netkeep login-item off                 # stop the agent and remove the plist; data and logs stay
NETKEEP_DATA=~/nk netkeep login-item on   # another data directory gets its own agent
```

The agent runs the `netkeep` you ran this from: uv's `~/.local/bin/netkeep` shim for a uv tool install, and otherwise the one beside the active Python, so a checkout's `.venv/bin/netkeep` after `make install`. It runs with `NETKEEP_DATA` set to the data directory in use. The default data directory's agent is `fun.tnkr.netkeep`; any other gets `fun.tnkr.netkeep.<hash of its path>`, so two data directories never replace each other's agent. The plist is `~/Library/LaunchAgents/<label>.plist`, outside the data directory: it is the one thing the switch writes there, and turning it off removes it.

If a `netkeep serve` is already running (the switch is always used from one), turning the item on registers it without starting it, so launchd starts it at your next login rather than racing the running copy for the serve lock. From a terminal with nothing running, `login-item on` starts it now. The agent that is serving you stops a moment after you turn the switch off.

Logs live under `<data_dir>/logs/`: `netkeep.log` is the application log, rotated by size (Settings, Logs), and the agent's `serve.log` and `serve.err.log` hold stdout and stderr's warnings, errors and crashes. Status: `launchctl print gui/$UID/<label>`. Each data directory keeps its Gmail secrets under its own Keychain service: `netkeep` for the default directory, and `netkeep@<hash of the path>` for any other, so a second data directory never reads or replaces the first one's Gmail sign-in. Set `NETKEEP_KEYCHAIN_SERVICE` to choose the service yourself; `netkeep config show` prints the one in use. Chrome is not managed by launchd. To start it at login too, turn on **Start netkeep's Chrome when netkeep starts** in Settings: `serve` then starts it once it is listening, or opens the UI in it when it is already running, unless a run holds the browser. The dashboard tells you when it isn't running, with a **Start Chrome** button.

## A plain note about LinkedIn's terms

LinkedIn's User Agreement prohibits automated access, including reading data it already shows you. netkeep reads only your own 1st-degree connections, ships conservative defaults, warms up slowly, backs off when LinkedIn pushes back, and never retries a security checkpoint. Those safeguards reduce the risk of an account restriction; they do not remove it. Read [the security and terms section](docs/architecture.md#18-security-privacy-and-terms-of-service) before you run it, and treat the daily budgets as the ceiling, not a target.

LinkedIn messages are prefilled by default: netkeep types the message into the conversation in your Chrome and stops, and you click **Send**. Auto-send is off by default. You turn it on with the **LinkedIn auto-send** switch on the Settings page, which asks you to confirm the risks first (or with `[campaigns] linkedin_auto_send = true` in `config.toml`, which then wins over the page), plus `auto_send` on each step that should use it. **Automated sending is what LinkedIn restricts accounts for fastest, and a restricted account stops the whole workflow, not just one step.** With auto-send on, netkeep clicks Send itself, and nobody reads the message before it goes. It sends only during your LinkedIn active hours, at least 20 minutes apart, within the `li_messages_auto` daily budget, never while heat (LinkedIn pushing back) is at its skip threshold, and only after it checks again that the message box belongs to the right person and holds exactly the rendered message. The posture page shows a warning for as long as it's on. Turning it off applies at once: an auto-send that hasn't clicked Send yet stops, and due steps wait for you to prefill them. Turning it on applies from the next auto-send check, within 10 minutes, with no restart. After LinkedIn's own answer shows a message went, netkeep closes that message bubble and its own tab; if anything else happens, it leaves the bubble open and holds auto-send until you close the bubble and resume it, on the LinkedIn queue or with `netkeep linkedin auto-send-resume`. An auto-send brings its Chrome tab to the front, as a prefill does, so keep your own typing out of that window, and keep LinkedIn's **Press Enter to Send** setting off (its default): with it on, an Enter you type into the composer could send part of a message. [ADR 0008](docs/adr/0008-auto-send.md) has the details. Run a handful of supervised prefills before you turn it on.

Enrichment opens each contact's LinkedIn profile from your account, so they may see a visit in **Who viewed your profile**. Whether they see your name and headline, a partial description (such as someone at your company), or an anonymous viewer depends on the Profile viewing options in LinkedIn's Visibility settings. netkeep never changes that setting, and it tells you this wherever enrichment starts or is enabled.

## Roadmap

| Phase | Delivers |
|---|---|
| 0 | Repository scaffold, config, database, CLI, API, frontend shell, CI |
| 1 | Import (LinkedIn archive, CSV), contacts, tags, lists, triage, exports |
| 2 | LinkedIn extractor: sync, enrichment, pacing, budgets, backoff |
| 3 | Email campaigns over Gmail: templates, sequences, review gate, reply detection |
| 4 | LinkedIn messaging: prefill by default, auto-send opt-in |
| 5 | Optional LLM module (after 1.0) |
| 6 | Backups and restore, polish, user guides, a template-drafting guide for your own AI subscription. Ends with the 1.0 release |
| 7 | Backlog after 1.0 |

The work runs in this order: phases 3, 4, then 6, which ends with the 1.0 release. Phase 5 and phase 7 come after 1.0.

Details and exit criteria are in [the spec](docs/architecture.md#19-delivery-phases).

## Documentation

User guides:

- [Upgrading from netkeeper](docs/guide/upgrading-from-netkeeper.md): the one-time move of your data, Keychain items and login item to the new name.
- [Setup](docs/guide/setup.md): install, the two Chrome windows, Gmail, your first import, triage, and your first LinkedIn sync.
- [Your first campaign](docs/guide/first-campaign.md): templates, an email campaign, and a LinkedIn step.
- [Backups and restore](docs/guide/backups.md).
- [Troubleshooting](docs/guide/troubleshooting.md): what a run's stop reason or a posture warning means, and the actions that stay on the command line.
- [Reporting a problem](docs/guide/reporting-a-problem.md): the support bundle to attach to an issue.
- [Gmail setup](docs/gmail-setup.md): your own Google Cloud OAuth client, step by step.
- [Import the old mailing tool's history](docs/history-import.md) (optional).
- [Importing into macOS Contacts](docs/macos-contacts.md).
- [Gmail drafts netkeep leaves behind](docs/gmail-deletes.md).
- [Draft templates with your AI assistant](docs/ai-drafting.md): use an AI chat subscription you already have to draft templates, without sending it your contacts' data.

Design and project:

- [Architecture spec](docs/architecture.md): design, data model, safety rules, phases.
- [Implementation guide](docs/implementation-guide.md): the backlog, what runs in parallel, and the human checkpoints.
- [Reconnect workflow](docs/networking-workflow.md): the method the tool automates.
- [Architecture decision records](docs/adr/): why the contested parts are the way they are.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for how the project works, and the [architecture decision records](docs/adr/) before proposing a change to the browser, Gmail, messaging, or user-scoping design. This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE).
