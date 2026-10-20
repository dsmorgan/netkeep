# Setup

This guide takes you from installing netkeep to a database full of your LinkedIn connections, sorted into people you've met and people you haven't. It takes an afternoon, most of it waiting for LinkedIn's data export. When you're done, go on to [your first campaign](first-campaign.md).

You use one Chrome for netkeep: **netkeep's Chrome**, a Chrome with its own profile and a debug port. It holds LinkedIn and setup steps such as Gmail linkage, and it's where you look for the UI, so you have one place to look ([ADR 0012](../adr/0012-chrome-profiles.md)). netkeep attaches to it to read LinkedIn and to type LinkedIn messages. netkeep starts it only when you ask: with **Start Chrome**, with `netkeep open`, or at startup if you turn that on. A run never starts a browser ([ADR 0002](../adr/0002-attach-only-browser-mode.md)).

The UI is at <http://127.0.0.1:8000>. `netkeep setup` opens it for you the first time; otherwise, open it yourself in Chrome. The UI requires Chrome.

## The Steps tab

The dashboard opens on **Steps** until setup is done: a diagram with a box for each step and an arrow from each step that has to come first. A green box is done, a highlighted box is the one to do now, and a faded box waits for the step its arrow comes from. Each box says in one line why, and links to the page where you do it. **Skip** marks a step you don't plan to do; it stays on the diagram, and **Un-skip** brings it back. Once every required step is done or skipped, the dashboard opens on **Overview**, and it remembers the tab you pick last.

## 1. Install

On macOS 14 or later, paste this into Terminal:

```sh
curl -fsSL https://github.com/dsmorgan/netkeep/releases/latest/download/install.sh | sh
```

It installs uv if you don't have it, installs netkeep with the dependency versions its release was tested with, then runs `netkeep setup`. It prints each step before it runs it, and `| sh -s -- --dry-run` prints the steps without running any. To read and verify the installer before you run it, follow [Download, verify, read, then run](../../README.md#download-verify-read-then-run) in the README, which also lists the [requirements](../../README.md#requirements).

`netkeep setup` turns on the login item, so `serve` starts whenever you log in, and opens the first-run page in netkeep's Chrome. If your release has no `netkeep setup` yet, the installer says so: start netkeep yourself with `netkeep serve`, leave it running in its own terminal (press Ctrl+C to stop it), and open <http://127.0.0.1:8000> in Chrome. You see an empty dashboard. `serve` creates and migrates the database itself.

If `netkeep` isn't found in a new terminal, the installer's last lines say what to do: run `~/.local/bin/uv tool update-shell` (by its full path, since `uv` may not be found either), or add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile (`~/.zshrc` for zsh), then open another terminal.

### What netkeep setup does

`netkeep setup` runs three steps and prints each one as it ends:

1. It turns on the login item, the same one as the **Start netkeep when I log in** switch in Settings, pointing at the installed `netkeep` (`~/.local/bin/netkeep` for a uv tool install). From a checkout, it points at `.venv/bin/netkeep` and says so.
2. It waits for `serve` to answer on 127.0.0.1, at `serve`'s port: 8000 by default, or the `--port` the login item was given.
3. It opens the first-run page in netkeep's Chrome: the UI's window and LinkedIn's sign-in page.

Run it again whenever you like: it says what's already done and changes nothing that's right. It uses the data directory every netkeep command uses, and never moves it. `netkeep setup --remove` turns the login item off and keeps your data. If a step fails, see [netkeep setup stops at a step](troubleshooting.md#netkeep-setup-stops-at-a-step).

### From a checkout

To work on netkeep itself, install the checkout prerequisites in the README's [From a checkout, for development](../../README.md#from-a-checkout-for-development), then:

```sh
git clone https://github.com/dsmorgan/netkeep.git
cd netkeep
make install
make build-ui
.venv/bin/netkeep serve
```

From a checkout, run the commands below as `.venv/bin/netkeep`, or activate the environment first with `source .venv/bin/activate`. To start `serve` when you log in, see [Run at login](../../README.md#run-at-login-macos).

### Updating

To update an install from the installer or `uv tool install`, run:

```sh
netkeep update --check   # only says whether a newer release exists
netkeep update           # updates to the latest release
netkeep update --version 1.3.0
```

netkeep never updates itself: only `netkeep update` installs anything, and `--check` changes nothing. `netkeep update` prints each step as it goes:

1. It downloads the new release's `constraints.txt` and the one for the version you have, over HTTPS from GitHub, and checks each against its release's `SHA256SUMS`. It keeps both in `<data>/update/`.
2. It stops the login item and waits until `serve` has let go of the serve lock. If you started `netkeep serve` yourself, it refuses: stop that `serve` first.
3. It backs up the database into `<data>/backups/`, as `netkeep backup` does.
4. It installs the release with `uv tool install`, with that release's constraints and uv's own Python 3.12.
5. It hands over to the new version, which starts the login item and checks that `serve` answers with the new version number.

If the install or that check fails, and the new version's `serve` hadn't started its scheduler yet, netkeep goes back by itself: it reinstalls the version you had, restores the backup with the login item off (when the new version had opened the database), starts the login item and checks again. If any `serve` had started its scheduler on this data directory during the update, it may have sent messages, and restoring the backup would forget them and could send them twice: netkeep never restores then, and never restores twice. Then netkeep stops, turns the login item off, and tells you what to do; see [Updating](troubleshooting.md#updating).

If the login item was off, `netkeep update` installs the new version and leaves it off; start netkeep with `netkeep serve`.

While an update is in progress, other netkeep commands refuse, so nothing else writes to the data the update backed up. `serve`, the recovery commands the update prints (`restore` and `login-item`), `version`, `--help` and a few read-only status commands still run.

A checkout doesn't update this way: run `git pull`, then `make install`.

## 2. Gmail

Follow [Gmail setup](../gmail-setup.md). In short: on **Settings**, under **Gmail**, the setup guide walks you through creating your own Google Cloud project and OAuth client, one step at a time, and ends with connecting your mailbox. Then, on the **Gmail** page, **Check now** runs a first check of the mailbox for replies.

Leave the mailbox disarmed for now. You arm it when you build your first campaign.

## 3. Your LinkedIn data export

Request your data from LinkedIn first, since it takes a while to arrive. The **Imports** page has the steps under **Getting your data from LinkedIn**: in LinkedIn, open **Settings & Privacy**, then **Data privacy**, then **Get a copy of your data**. Ask for your full data archive, not just Connections: netkeep also reads your message and invitation history. LinkedIn emails you a download link, usually within a day. Save the `.zip`.

## 4. Import it

1. Open **Imports**. Choose **Choose a file** (or drop the file on the page) and pick the `.zip`. You don't need to unzip it.
2. netkeep says what it recognized, such as "Recognized: a LinkedIn data archive", and what it would add.
3. Choose **Import**. The result lists new contacts, updated contacts, and the conversations and invitations it added.

From a terminal, `netkeep import archive ~/Downloads/your-archive.zip` does the same.

You can also import a CSV from another source. On **Imports**, choose a `.csv`, map its columns under **Map the columns**, choose **Preview the import**, review any candidates (rows that may be people you already have), then commit.

To undo an import, open **Imports**, then **History**, open the import, and choose **Roll back this import**. It deletes only what that import created and restores what it changed. Undo later imports first.

## 5. Triage

**Triage** asks one question about each contact: have you actually met this person? The evidence panel shows what netkeep knows, such as messages you exchanged and employers you share.

| Key | What it does |
|---|---|
| `m` | Met this person |
| `n` | Have not met them |
| `s` | Skip; revisit later with the **Skipped** filter |
| `p` | Edit the preferred name, the name a campaign addresses them by |
| `t` | Tag this contact |
| `←` `→` | Go back or forward without deciding |
| `u` | Undo the last thing written |
| `?` | Show or hide every key |

Then use **Contacts** and **Lists** to sort further. A static list holds the people you add to it; a smart list holds everyone who matches a filter. Campaigns send to a list or a tag. **Exports** writes CSV, JSON, or a vCard; see [Importing into macOS Contacts](../macos-contacts.md).

## 6. The LinkedIn Chrome

1. Start it. On the **LinkedIn** page's **Browser** card, choose **Start Chrome**. netkeep starts its own Chrome, on its own profile and the debug address in Settings, with netkeep in its own window, and says how it went. It never starts or uses any other Chrome: when another Chrome holds the debug port, or a run is using the browser, it starts nothing and says what to do. From a terminal, `netkeep open` does the same. You can also run `make chrome` from the repository, or the command `netkeep browser launch` prints; the card shows that command too.
2. In that new window, log in to LinkedIn once. netkeep never types your password; you sign in yourself. Use this window for your own LinkedIn browsing too, so your activity and netkeep's share one session.
3. Check it: `netkeep preflight`. It attaches, opens one blank tab, reads the names of the profile's LinkedIn cookies and a few browser properties, and closes the tab. It visits no website.

Start netkeep's Chrome again whenever you restart your Mac or quit it. The dashboard tells you when it isn't running, with a **Start Chrome** button. To have it start by itself, turn on **Start netkeep's Chrome when netkeep starts** in Settings, under **LinkedIn runs**. It's off by default. With [Run at login](../../README.md#run-at-login-macos) also on, Chrome then starts when you log in. When its Chrome is already running, restarting `serve` brings netkeep's window there to the front, or opens one when none is open, except within 5 minutes of the last time it did that. It starts nothing when a run is using the browser as `serve` starts.

Read [the note about LinkedIn's terms](../../README.md#a-plain-note-about-linkedins-terms) before your first run.

## 7. Your first sync and enrichment

On the **LinkedIn** page:

1. Under **Start a run**, pick **Full connections sync** and choose **Start run**. It pauses between pages at a human pace. The dashboard's live run card and the tab's icon show it running. When it ends, click its row under **Runs** to see how far it got and why it stopped.
2. Then start an **Enrichment**, with a small **Max visits** the first time. Enrichment opens each contact's profile, so they may see your visit in **Who viewed your profile**; the page tells you what LinkedIn shows them.
3. Choose **Check inbox now** under **LinkedIn inbox**. The first inbox poll always runs by hand; scheduled polls wait for it.

When the manual runs look right, choose **Arm scheduled runs** under **Scheduled runs**. From then on, `serve` syncs and enriches on its own, inside your LinkedIn active hours and daily budgets. **Pause schedule** holds it; **Disarm** stops it.

Every protection and its state is on **Settings**, under **Posture**. "NOT clear" there means a warning needs you; see [Troubleshooting](troubleshooting.md).

## 8. Settings

Go through **Settings** once:

- **Sending hours**: when campaign email may go out.
- **About you**: your name, title, and company, for templates.
- **LinkedIn budgets** and **LinkedIn active hours**: how much netkeep does on LinkedIn, and when. The defaults are conservative; treat them as the ceiling.
- **Campaign defaults**: daily caps and spacing.
- **Backups** and **Backup files**: nightly backups (on by default), how many to keep, and **Back up now**. See [Backups and restore](backups.md).

A value in `config.toml`, if you have one, wins over the page.

## Optional: the old mailing tool's history

If you emailed these people before with another tool, import that history before your first campaign, so netkeep's guards know whom you already reached and who asked to be left alone. See [Import the old mailing tool's history](../history-import.md).
