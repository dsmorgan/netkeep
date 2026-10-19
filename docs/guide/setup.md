# Setup

This guide takes you from a fresh clone to a database full of your LinkedIn connections, sorted into people you've met and people you haven't. It takes an afternoon, most of it waiting for LinkedIn's data export. When you're done, go on to [your first campaign](first-campaign.md).

You use one Chrome for netkeep: **netkeep's Chrome**, a Chrome with its own profile and a debug port. It holds LinkedIn and setup steps such as Gmail linkage, and it's where you look for the UI, so you have one place to look ([ADR 0012](../adr/0012-chrome-profiles.md)). netkeep attaches to it to read LinkedIn and to type LinkedIn messages. netkeep starts it only when you ask: with **Start Chrome**, with `netkeep open`, or at startup if you turn that on. A run never starts a browser ([ADR 0002](../adr/0002-attach-only-browser-mode.md)).

The first time, you open the UI at <http://127.0.0.1:8000> yourself, in Chrome. The UI requires Chrome.

## The Steps tab

The dashboard opens on **Steps** until setup is done: a diagram with a box for each step and an arrow from each step that has to come first. A green box is done, a highlighted box is the one to do now, and a faded box waits for the step its arrow comes from. Each box says in one line why, and links to the page where you do it. **Skip** marks a step you don't plan to do; it stays on the diagram, and **Un-skip** brings it back. Once every required step is done or skipped, the dashboard opens on **Overview**, and it remembers the tab you pick last.

## 1. Install

Install the prerequisites in the README's [Requirements](../../README.md#requirements), then:

```sh
git clone https://github.com/dsmorgan/netkeep.git
cd netkeep
make install
make build-ui
.venv/bin/netkeep serve
```

Open <http://127.0.0.1:8000> in Chrome. You see an empty dashboard. `serve` creates and migrates the database itself. Leave it running in its own terminal; press Ctrl+C to stop it.

The commands below use `netkeep` for short. Run them as `.venv/bin/netkeep`, or activate the environment first with `source .venv/bin/activate`.

To start `serve` when you log in, see [Run at login](../../README.md#run-at-login-macos).

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

Start netkeep's Chrome again whenever you restart your Mac or quit it. The dashboard tells you when it isn't running, with a **Start Chrome** button. To have it start by itself, turn on **Start netkeep's Chrome when netkeep starts** in Settings, under **LinkedIn runs**. It's off by default. With [Run at login](../../README.md#run-at-login-macos) also on, Chrome then starts when you log in. When its Chrome is already running, restarting `serve` opens netkeep in a new window there, so each restart adds a window, except within 5 minutes of the last one it opened that way. It starts nothing when a run is using the browser as `serve` starts.

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
