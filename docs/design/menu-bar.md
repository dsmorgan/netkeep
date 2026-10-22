# The menu-bar app

This note holds the mechanics behind [ADR 0014](../adr/0014-menu-bar-status-app.md), which holds the decision. It was written for #755 on 2026-10-22, before any implementation. Each implementation issue checks it against the code and updates it.

## Status

- `serve` serves one read-only endpoint, `GET /api/v1/system-status` (#761), with the server-side parts of the three chips (#751), rolled up into a tone, a word, and a fact for each chip. The UI's chips read it. The rules live in `netkeep/services/system_status.py`; the menu bar imports its `roll_up` and `NETKEEP_LEAD` to add its own part to the netkeep chip.
- Besides the chips, it returns `linkedin_paused` (Pause or Resume), `linkedin_run` (the run in progress, with its name and start), `campaign_sending` (a campaign tick in flight in this process, which may be sending), and `update_available` (null until #762 fills it). The Update and Quit confirmations name the run and the batch.
- **Times are timestamps, never text.** A fact or a detail that says when holds a `{name}` placeholder, and the chip's `times` maps each name to an absolute UTC time (ISO 8601) and a style: `ago` ("3 min ago"), `next` ("in 7 min", "14:20", "Tue 14:20") or `when` (a date and time in the reader's locale). Each client writes them when it renders: the UI with its existing formatters, the menu bar each time the menu opens. A cached answer then never shows a stale "2 min ago" (maintainer, 2026-10-22).
- A read that fails on the server fails only its own part, which says "Couldn't check". The netkeep chip in the answer holds only the server's parts (background checks, and the Keychain when it refused the key), so each client rolls it up again with its own.
- Two moments the database doesn't record come from this process's memory: a run's live progress (the latest `run.progress` event) and a campaign send batch (shown for 15 s after the `campaign.tick` event of a tick that sent).
- Each client adds its own connection parts on the same scale (`ok`, `off`, `busy`, `warn`, `bad`, worst wins):
  - **The UI:** server reachable, and live updates (the event stream).
  - **The menu bar:** server reachable. A failed read shows "Unreachable" on the netkeep line, and "Unknown" on Gmail and LinkedIn, as in the UI.
- The menu bar polls on a modest interval. The icon's tint is the worst of the three chips.

## Authentication

- The menu bar reads the install key (`netkeep/install/<name>`, ADR 0013) once at startup and keeps it in memory. It sends the key as `Authorization: Bearer`, only to the host and port in the login item's plist.
- When the Keychain refuses, or `serve` answers `401` (it's running on a key held only in memory), the icon shows `off` and the menu gets a line that says why. The menu bar reads the key again only when you open the menu.

## Actions

| Menu item | What it calls |
|---|---|
| Open netkeep | `netkeep open` (the launcher's path, below) |
| Check replies now | `POST /poll-status/gmail-replies/check-now` |
| Pause / Resume LinkedIn scheduled runs | `POST /linkedin/schedule/pause`, `/unpause` |
| Update to X.Y.Z | `netkeep update`, detached, after a confirmation |
| Quit netkeep | `LoginItem.stop()`, then the menu bar exits, after a confirmation |

A test pins this table: the menu-bar module may call nothing else. The confirmations for Update and Quit name any LinkedIn run or campaign batch in progress.

## Update

- The menu bar imports everything it uses at startup, before any update can replace the tool environment.
- It starts `netkeep update` in its own session, so the update outlives the menu bar, and it watches the update's state file (`state.json` in the data directory's update folder, `netkeep/services/update.py`). It shows each phase and any refusal.
- `update --finish` restarts the menu-bar job last, after its health check, so the menu bar runs the new code.

## Jobs, the preference, and Quit

- **Labels:** the menu-bar job's label is the login item's label (`fun.tnkr.netkeep`, or `fun.tnkr.netkeep.<hash>` for another data directory) plus a fixed `.menubar` suffix. It runs the helper bundle's script. `KeepAlive` uses `SuccessfulExit: false`, so launchd restarts the menu bar after a crash but not after Quit.
- **The preference:** **Show menu-bar icon** is stored like the other Settings values, in `settings_kv`, default on. The plist alone can't hold it, because Run at login off removes both plists.

| Action | Preference on | Preference off |
|---|---|---|
| Run at login on, or `netkeep setup` | Adds both jobs | Adds the `serve` job; removes the menu-bar job |
| Run at login off, or `setup --remove` | Removes both | Removes both |
| `update --finish` | Restarts both | Starts `serve` only |
| Turning the preference on or off | Adds or removes the menu-bar job, if Run at login is on | |

- **Quit:** `LoginItem.stop()` boots out the `serve` job and leaves its plist. Then the menu bar exits with status 0. Both come back at the next login.
- **The launcher (#754):** for each job whose plist exists, it bootstraps the job if `launchctl print` doesn't find it, then kickstarts it. After that it waits for `/health` and opens the window. So **Open netkeep** brings both processes back after a Quit.
