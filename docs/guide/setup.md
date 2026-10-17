# Setup

This guide takes you from a fresh clone to a database full of your LinkedIn connections, sorted into people you've met and people you haven't. It takes an afternoon, most of it waiting for LinkedIn's data export. When you're done, go on to [your first campaign](first-campaign.md).

You use two Chrome windows, and they are different:

- **The UI's Chrome** is your everyday Google Chrome, where you open netkeeper at <http://127.0.0.1:8000>. netkeeper's UI requires Chrome.
- **The LinkedIn Chrome** is a second Chrome window, with its own profile and a debug port, that you start yourself. netkeeper attaches to it to read LinkedIn and to type LinkedIn messages. It never starts a browser itself ([ADR 0002](../adr/0002-attach-only-browser-mode.md)).

## 1. Install

Install the prerequisites in the README's [Requirements](../../README.md#requirements), then:

```sh
git clone https://github.com/dsmorgan/netkeeper.git
cd netkeeper
make install
make build-ui
.venv/bin/netkeeper serve
```

Open <http://127.0.0.1:8000> in Chrome. You see an empty dashboard. `serve` creates and migrates the database itself. Leave it running in its own terminal; press Ctrl+C to stop it.

The commands below use `netkeeper` for short. Run them as `.venv/bin/netkeeper`, or activate the environment first with `source .venv/bin/activate`.

To start `serve` when you log in, see [Run at login](../../README.md#run-at-login-macos).

## 2. Gmail

Follow [Gmail setup](../gmail-setup.md). In short: on **Settings**, under **Gmail**, the setup guide walks you through creating your own Google Cloud project and OAuth client, one step at a time, and ends with connecting your mailbox. Then, on the **Gmail** page, **Check now** runs a first check of the mailbox for replies.

Leave the mailbox disarmed for now. You arm it when you build your first campaign.

## 3. Your LinkedIn data export

Request your data from LinkedIn first, since it takes a while to arrive. The **Imports** page has the steps under **Getting your data from LinkedIn**: in LinkedIn, open **Settings & Privacy**, then **Data privacy**, then **Get a copy of your data**. Ask for your full data archive, not just Connections: netkeeper also reads your message and invitation history. LinkedIn emails you a download link, usually within a day. Save the `.zip`.

## 4. Import it

1. Open **Imports**. Choose **Choose a file** (or drop the file on the page) and pick the `.zip`. You don't need to unzip it.
2. netkeeper says what it recognized, such as "Recognized: a LinkedIn data archive", and what it would add.
3. Choose **Import**. The result lists new contacts, updated contacts, and the conversations and invitations it added.

From a terminal, `netkeeper import archive ~/Downloads/your-archive.zip` does the same.

You can also import a CSV from another source. On **Imports**, choose a `.csv`, map its columns under **Map the columns**, choose **Preview the import**, review any candidates (rows that may be people you already have), then commit.

To undo an import, open **Imports**, then **History**, open the import, and choose **Roll back this import**. It deletes only what that import created and restores what it changed. Undo later imports first.

## 5. Triage

**Triage** asks one question about each contact: have you actually met this person? The evidence panel shows what netkeeper knows, such as messages you exchanged and employers you share.

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

1. Start it. Either run `make chrome` from the repository, or run `netkeeper browser launch` and copy the command it prints. The **LinkedIn** page's **Browser** card shows the same command.
2. In that new window, log in to LinkedIn once. Use this window for your own LinkedIn browsing too, so your activity and netkeeper's share one session.
3. Check it: `netkeeper preflight`. It attaches, opens one blank tab, reads the names of the profile's LinkedIn cookies and a few browser properties, and closes the tab. It visits no website.

Start the LinkedIn Chrome again whenever you restart your Mac or quit it. The dashboard tells you when netkeeper can't reach it.

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
- **LinkedIn budgets** and **LinkedIn active hours**: how much netkeeper does on LinkedIn, and when. The defaults are conservative; treat them as the ceiling.
- **Campaign defaults**: daily caps and spacing.
- **Backups** and **Backup files**: nightly backups (on by default), how many to keep, and **Back up now**. See [Backups and restore](backups.md).

A value in `config.toml`, if you have one, wins over the page.

## Optional: the old mailing tool's history

If you emailed these people before with another tool, import that history before your first campaign, so netkeeper's guards know whom you already reached and who asked to be left alone. See [Import the old mailing tool's history](../history-import.md).
