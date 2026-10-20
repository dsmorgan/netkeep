# Troubleshooting

Start with **Settings**, under **Posture**, or `netkeep posture --no-probe` in a terminal. Each line is one protection and its state. "NOT clear" means at least one has a warning, and the warning says what to do. **Show details** (or `--details`) shows the rest. Without `--no-probe`, `netkeep posture` also attaches to the LinkedIn Chrome to check the session, as `netkeep preflight` does.

For a LinkedIn run, open the **LinkedIn** page and click the run's row under **Runs**. It says how the run ended, in plain words, and for enrichment, which visits couldn't be read and why. `netkeep linkedin runs` and `netkeep linkedin run <id>` show the same.

For Gmail, see the reason codes in [Gmail setup](../gmail-setup.md#troubleshooting).

Still stuck? Open an issue with a support bundle and screenshots: see [Reporting a problem](reporting-a-problem.md).

## Installing and opening the UI

| What you see | What to do |
|---|---|
| "both ... hold a database; netkeep won't pick one" | Both `.../netkeeper` and `.../netkeep` hold a database, usually after going back to netkeeper by restoring a backup. The message shows when each was last written. Move the one you don't want aside, then start again. If the login item runs netkeep, it retries every 10 seconds and the message is in `<data>/logs/serve.err.log`. |
| "netkeep can't move netkeeper's data yet" | The one-time move to the new name refused. The message says why; [Upgrading from netkeeper](upgrading-from-netkeeper.md#when-it-refuses) lists each case. |
| The browser asks you to connect after the upgrade | Run `netkeep open`. The rename changed the cookie's name, and the old one works only until the next release. |
| "The frontend is not built" | Run `make build-ui`, then reload. Run it again after each `git pull`. |
| A banner that says the UI requires Chrome | Open <http://127.0.0.1:8000> in Google Chrome. |
| "Backend unreachable" in the header | `serve` stopped. Start it again: `.venv/bin/netkeep serve`. |
| `serve` says the address is already in use | Another `serve`, or another program, has port 8000. Stop it, or run `netkeep serve --port 8080`. |

## The LinkedIn Chrome

| What you see | What to do |
|---|---|
| "Chrome was not reachable or went away mid-run" | Start netkeep's Chrome: **Start Chrome** on the dashboard or the **LinkedIn** page, `netkeep open`, `make chrome`, or the command `netkeep browser launch` prints. Then `netkeep preflight`. |
| **Start Chrome** or `netkeep open` opens another netkeep window | When netkeep's window is already open in netkeep's Chrome, they bring it to the front instead, and say so. They open another only when netkeep can't find one there, or can't bring it forward: while a run is using the browser, on setup's first run, or when the check fails. A window opened on another address (another port, or `https`) doesn't count. When the check fails, `netkeep.log` has a "could not bring netkeep's window to the front" line. |
| You closed netkeep's window, and Chrome is still running (on macOS) | On macOS, closing Chrome's last window leaves Chrome running, with no windows. Run `netkeep open` (or **Start Chrome**) to reopen the window in that same Chrome. A run that starts while it has no windows stops with "Chrome was not reachable", and its detail says "it has no windows open": reopen the window, then run it again. |
| **Start Chrome**: "Another Chrome is using netkeep's debug port" | A Chrome that isn't netkeep's answers on the debug address, so netkeep started nothing. Quit that Chrome, or change **Chrome debug address** in Settings, under **LinkedIn runs**, then try again. |
| **Start Chrome**: "A Chrome has netkeep's profile open without the debug port" | netkeep's profile is open in a Chrome started without the port, so runs couldn't attach to it. Quit that Chrome, then try again. If no Chrome is running, a crash left the profile's lock behind: `make chrome` clears it. |
| **Start Chrome** is disabled: "A run is using the browser" | A run holds the browser. Try again when it ends. |
| **Start Chrome**: "points at another computer" or "has no valid port" | Fix **Chrome debug address** in Settings, under **LinkedIn runs**: netkeep starts Chrome only on this computer, on the port you set. |
| **Start Chrome**: "Chrome didn't start" | Check that Google Chrome is installed (on macOS, in `/Applications`). The message under it says what Chrome answered. |
| Chrome doesn't start at login, or no window opens when `serve` restarts | Check that both **Run at login** and **Start netkeep's Chrome when netkeep starts** are on in Settings. When netkeep's Chrome is already running, `serve` brings netkeep's window there to the front, or opens one when none is open. Within 5 minutes of the last time it did that, a restart does nothing: choose **Start Chrome**. netkeep starts nothing when a run is using the browser as `serve` starts, or while a launch is under way. `netkeep.log` has a `start netkeep's Chrome at startup` line with the outcome, or says why it started nothing. If the outcome is `refused (profile_without_port)` when no Chrome is running, a crash left the profile's lock behind: `make chrome` clears it. |
| Chrome ignores the debug port | Chrome 136 and later refuse it on your default profile. Use the command netkeep prints, which has its own `--user-data-dir`. |
| Chrome won't start on the profile after a crash | `make chrome` clears the profile lock a crashed Chrome left behind. |
| "the browser was busy with another run" | One netkeep process uses the browser at a time. `netkeep preflight` names the process that holds it. Wait for that run, or stop it. |
| A **Connection check** run in the runs list, "read the LinkedIn session from Chrome's cookie jar; visited nothing" | A check of the LinkedIn Chrome and its session (`POST /api/v1/linkedin/browser/check`), like `netkeep preflight`. It opens no page and spends no budget. Its result is the session line on the run and on **Settings**, under **Posture**. If it ended "Chrome was not reachable" or "isn't netkeep's own profile", start the LinkedIn Chrome with `make chrome` and check again. |
| Posture: the activity lock "is probably stuck", or is held by something netkeep can't name | Stop the process the warning names. netkeep releases the lock when its holder exits. |

## Why a LinkedIn run stopped

netkeep classifies every answer LinkedIn gives (spec [9.7](../architecture.md#97-failure-classification-and-heat)). These are the endings that need you; the rest, such as "read to the end of the connections list" or "made the visits it was allowed", are normal.

| The run says | What it means | What to do |
|---|---|---|
| LinkedIn throttled it | LinkedIn pushed back (HTTP 429 or 999). netkeep raised heat and slowed down. | Nothing. Heat decays on its own, and runs resume. The **LinkedIn** page shows when. Use **Clear heat…** only if you know the block was something else. |
| LinkedIn showed a checkpoint | LinkedIn wants you to prove you're you. netkeep set the session flag and stopped. It never retries a checkpoint. | In the LinkedIn Chrome, open LinkedIn yourself and resolve it. Then choose **Clear flag…** on the banner, or run `netkeep linkedin clear-flag`. |
| LinkedIn asked to log in | The session expired or was logged out. netkeep set the session flag. | Log in to LinkedIn in the LinkedIn Chrome, then run `netkeep preflight`. It clears the flag once it sees a live session. |
| the page's answers changed shape | LinkedIn served a page netkeep doesn't recognize. It gave up on that page for the run. | Run once more by hand. Two connections runs in a row ending this way trip the route-changed breaker (below). If it keeps happening, LinkedIn changed something; open an issue. |
| lost some of the page's answers | The page loaded, but Chrome didn't keep some answers' bodies. | Nothing, once. Repeated losses trip the answer-lost limit (below). |
| too many profiles were not found | Several profiles in a row were missing. None was recorded as not found. | Nothing. They're visited again later. |
| refused: the session is flagged | A checkpoint or login wall set the session flag earlier. | Clear it as above. |
| refused: heat is over its skip threshold | Heat is too high for browser jobs. | Wait. The **LinkedIn** page shows when runs resume. |
| outside active hours, or today's or this week's budget is spent | A limit you set. | Nothing, or change it on **Settings**, under **LinkedIn active hours** or **LinkedIn budgets**. |
| refused: scheduled runs are disarmed | The schedule is off. | **Arm scheduled runs** on the **LinkedIn** page, when you're ready. |
| refused: the first LinkedIn inbox poll is run by hand | Scheduled inbox polls wait for one you start. | **Check inbox now** on the **LinkedIn** page, or `netkeep linkedin inbox`. |
| refused: the route-changed breaker, the answer-lost limit, the Contact info breaker, the inbox breaker, or the inbox owner breaker is tripped | Repeated failures of one kind stopped scheduled runs of that kind. Runs you start by hand still go. | Start one run of that kind by hand. One that reaches its end clears the breaker. Or, without a run, choose **Reset breaker** on the tripped breaker's row on **Settings**, under **Posture**, or run `netkeep linkedin schedule reset-breaker`. |
| the page showed another LinkedIn mailbox than this account's | The LinkedIn Chrome is signed in to a different LinkedIn account. | Sign back in to yours. If you changed accounts on purpose, choose **Forget inbox owner** on **Settings**, under **Posture**, or run `netkeep linkedin inbox-forget-owner`. Scheduled inbox polls then wait until you poll by hand (**Check inbox now** on the **LinkedIn** page, or `netkeep linkedin inbox`) while Chrome is signed in to the new account; that poll records it. |
| refused: the inbox owner was forgotten, so the poll that records it again is run by hand | You chose **Forget inbox owner** (or ran `netkeep linkedin inbox-forget-owner`), and no poll you started has recorded the mailbox since. | Check that Chrome is signed in to your LinkedIn account, then **Check inbox now** on the **LinkedIn** page, or `netkeep linkedin inbox`. A poll that sees no conversation records no mailbox, so the hold stays; poll again once a conversation shows in your LinkedIn inbox. |
| the first poll could not read back to the earliest outreach | The first LinkedIn inbox poll couldn't reach your oldest watched outreach, so some older replies may be missed. | Check older LinkedIn replies by hand, then choose **Acknowledge** on **Settings**, under **Posture**, or run `netkeep linkedin inbox-acknowledge`. |
| an error stopped it | Something unexpected went wrong. | Check `serve`'s log (`<data dir>/logs/serve.err.log` under launchd, or its terminal), then run once by hand. If it repeats, open an issue with the log lines. |
| the netkeep process running it stopped | `serve` stopped or restarted mid-run. | Start the run again. An enrichment can **Resume** its plan once, from the run's detail. |

## Posture warnings

Each warning starts with the protection it belongs to. These are the ones you're most likely to see, quoted from the report:

| The warning says | What to do |
|---|---|
| "nothing has checked the LinkedIn session yet" | Run `netkeep preflight`. |
| "the last check (…) found no LinkedIn session in this Chrome profile, so no job can run" | Log in to LinkedIn in the LinkedIn Chrome, then run `netkeep preflight`. |
| "the session was last confirmed …, more than 24 h ago, so it may have expired since" | Run `netkeep preflight` again. |
| "LinkedIn answered with a checkpoint on …" or "LinkedIn answered with a logged out on …" | The session flag is up. Clear it as in the table above. |
| "scheduled jobs: no job kind has a due time for this account, so nothing fires on its own" | Expected until `serve` has run with scheduled runs armed. |
| "the first LinkedIn inbox poll couldn't read back to …" | Check older LinkedIn replies by hand, then choose **Acknowledge** under **Posture**. |
| "LinkedIn auto-send is on (campaigns.linkedin_auto_send)" | You turned on LinkedIn auto-send. Unless you mean it, turn it off on the Settings page, or in `config.toml` when the file sets it (the page shows it locked then). Off applies at once. |
| "linkedin.pacing.… is …", or "linkedin.cdp_url points at …, not this machine's loopback" | A value in `config.toml` weakens a protection. Remove it to get the default back. |
| "the last complete full sync (run …) refused to age anyone" | A sync would have marked too many connections gone at once, so it marked none. Check the run it names. The warning clears after a later complete full sync. |

## Gmail

| What you see | What to do |
|---|---|
| "Gmail needs you to sign in again" | Choose **Re-authorize** on the banner, or run `netkeep gmail login`. Email steps wait until you do. |
| **Arm to send** says "no netkeep test draft was found in the Drafts of … at the last check" | Make a test draft from a campaign's review (**Send a test of step 1**) while the mailbox is armed for drafts, then **Check now**. The arm steps say "netkeep found its own draft." when it's found. |
| A step's email went out as a draft | The mailbox is armed for drafts only. Send the draft yourself, or **Arm to send**. |

## netkeep setup stops at a step

`netkeep setup` runs three steps and stops at the first one that fails, with a line that starts "to fix it:". It exits nonzero, so the installer stops there too. Fix the problem, then run `netkeep setup` again: it skips what's already done.

| Step that failed | What you see | What to do |
|---|---|---|
| 1. The login item | "Run at login uses launchd, which only macOS has." | On Linux, run netkeep in its container instead. |
| 1. The login item | "can't find the installed netkeep command" | Install netkeep with the installer in the README, then run `netkeep setup` again. |
| 1. The login item | "launchctl ... failed" | `netkeep login-item status` shows where it stands. Nothing is left half-installed. |
| 2. Waiting for serve | "netkeep serve didn't answer ... within 60 s" | Look in `<data>/logs/serve.err.log` for why it didn't start, for example another program on `serve`'s port (8000 by default). A slow first start can take longer: run `netkeep setup --wait 180`. |
| 3. The first-run page | "Another Chrome holds netkeep's debugging port" | Quit that Chrome, then run `netkeep setup` again. |
| 3. The first-run page | "the Keychain refused the local API key" | Unlock your login keychain, or allow netkeep when macOS asks. |

Setup prints a note when the login item can't use uv's `~/.local/bin/netkeep` shim: when it runs from a checkout's `.venv`, or from a uv tool install whose shim is missing (`uv tool install --reinstall netkeep` puts it back). The login item then runs that environment's own `netkeep`, and stops working if you move or delete the folder. When netkeep's Chrome is already running, step 3 opens no other window; run `netkeep open` to bring up the UI. Step 2 can't tell this data directory's `serve` from another netkeep's on the same port, because `/api/v1/health` names no data directory: if you run two data directories, give each its own `web.port`.

`netkeep setup --remove` turns the login item off. Your data directory, logs and Keychain items stay.

## Updating

`netkeep update` prints each step and stops at the first one that fails. Until it has installed anything, a failure changes nothing: the login item starts again and you're on the version you had.

| What you see | What happened | What to do |
|---|---|---|
| "this netkeep runs from a checkout" | `netkeep update` replaces only an installer or `uv tool install` install. | Run `git pull`, then `make install`. |
| "... holds the serve lock, and it isn't the login item's serve" | You started `netkeep serve` yourself, or a `netkeep restore` is running. | Stop that `serve` (Ctrl+C in its terminal), or wait for the restore, then run `netkeep update` again. |
| "constraints.txt has SHA-256 ..., but its SHA256SUMS says ..." | A download didn't match its release's checksum. Nothing was installed. | Run it again later. If it keeps happening, [report it](reporting-a-problem.md). |
| "Rolled back: netkeep X is running again" | The new version failed before its scheduler started, so netkeep put back the version and the database you had. | Look in `<data>/logs/serve.err.log` for why, and [report it](reporting-a-problem.md). |
| "The update stopped without rolling back" | A `serve` started its scheduler on this data directory during the update, so it may have sent messages; or the new version's `serve` wouldn't stop. Its login item is off. | See below. |
| "an update from X to Y didn't finish (installing)" | netkeep stopped while `uv` was installing the new version, for example in a crash or a power cut. | Run `netkeep version`. If it prints the new version, run `netkeep update --finish`. Otherwise reinstall the version you had with its kept constraints: `uv tool install --python 3.12 --managed-python -c <data>/update/constraints-X.txt netkeep==X --reinstall`. Then run `netkeep login-item on`, check netkeep works, and delete `<data>/update/state.json`. Nothing was restored, and the new version never ran. |
| "an update from X to Y didn't finish (rolling-back)" | The old version's reinstall failed during a rollback. | Run the `uv tool install` command it printed, then `netkeep update --rollback`. It restores the backup only if no `serve` has started its scheduler since; otherwise it stops and explains. |
| "an update from X to Y didn't finish (stopped)" | An earlier update stopped and explained why. | Follow what it printed (below). Once netkeep runs the way you want, delete `<data>/update/state.json` and run `netkeep update` again. |
| "an update is in progress: run `netkeep update --finish` or see troubleshooting" | While `<data>/update/state.json` exists, every command refuses except `netkeep update` and its steps, `netkeep version`, `--help`, and the status commands `login-item status`, `db current`, `backup list` and `config show`. | Finish or recover the update with the row above that matches its phase; `netkeep update` names the phase. |
| "this rollback already restored the backup" | `netkeep update --rollback` ran a second time. It never restores twice, because the restored version may have sent since. | Run `netkeep login-item on`, check netkeep works, then delete `<data>/update/state.json`. |

When the update stops without rolling back, you have two choices:

- Keep the new version. Look in `<data>/logs/serve.err.log` for why it failed, fix that, then run `netkeep login-item on`.
- Go back to the version you had, knowing that whatever the new version sent or recorded since the backup is forgotten, and could be sent again. Run the `uv tool install ...` command it printed, then `netkeep restore <backup>` with the backup it named, then `netkeep login-item on`.

Then delete `<data>/update/state.json`.

## Run at login does nothing

Turning on **Start netkeep when I log in** while netkeep is running only registers the login agent. Nothing changes until your next login: netkeep never starts a second copy next to the one you are using. The card's state says where you are:

| State | Meaning | What to do |
|---|---|---|
| Off | No login agent for this data directory, or launchd has it turned off (for example in Login Items in System Settings). | Turn the switch on. |
| Registered: starts at next login | The agent is in place but launchd hasn't started it. | Log out and back in, then open netkeep's bookmark without starting anything. |
| Running from login agent | launchd started this netkeep. | Nothing. |
| Needs repair | An agent from the removed install script serves this data directory and can't start. | **Repair** on the card, or `netkeep login-item on` in a terminal. |
| Points at a different netkeep | The agent runs another copy than the one you are using, such as an old checkout. | **Repair** on the card. |

`netkeep login-item status` prints the same state names. To ask launchd directly, run this in a terminal. The label is on the card, and `NETKEEP_DATA=<dir> netkeep login-item status` prints it on its `label:` line. The default data directory's is `fun.tnkr.netkeep`; any other has a hash after it:

```sh
launchctl print gui/$(id -u)/<label>
```

For example, `launchctl print gui/$(id -u)/fun.tnkr.netkeep`.

`state = running` and a `pid` mean launchd started it. "Could not find service" means the agent isn't loaded: log out and back in, or run `netkeep login-item on` with netkeep stopped to load it now. `<data dir>/logs/serve.err.log` holds the agent's crashes.

## Actions that stay on the command line

Everything else is in the UI. These remain in a terminal or a file, most of them by design:

| Action | How |
|---|---|
| Start `serve` at login | Settings, Run at login, or `netkeep login-item on`. See [Run at login](../../README.md#run-at-login-macos), and [if it does nothing](#run-at-login-does-nothing). Two data directories each need their own `web.port`: the second `serve` can't bind a port the first holds, and its login item would restart it every 10 seconds. |
| Check the LinkedIn Chrome (optional) | `netkeep preflight`. |
| Turn on LinkedIn auto-send | Settings, **Campaign defaults**, **LinkedIn auto-send**, and confirm. It applies from the next auto-send check, within 10 minutes. `config.toml`'s `[campaigns] linkedin_auto_send` still works and wins over the page. Read [ADR 0008](../adr/0008-auto-send.md) first. |
| Import the old mailing tool's history (optional) | `netkeep history import` and `netkeep history scan-gmail`. See [the guide](../history-import.md). |
| Restore a backup | `netkeep restore <name or path>`, with `serve` stopped. The **Backup files** card in Settings shows each backup's command. See [Backups and restore](backups.md). |
