# Troubleshooting

Start with **Settings**, under **Posture**, or `netkeeper posture --no-probe` in a terminal. Each line is one protection and its state. "NOT clear" means at least one has a warning, and the warning says what to do. **Show details** (or `--details`) shows the rest. Without `--no-probe`, `netkeeper posture` also attaches to the LinkedIn Chrome to check the session, as `netkeeper preflight` does.

For a LinkedIn run, open the **LinkedIn** page and click the run's row under **Runs**. It says how the run ended, in plain words, and for enrichment, which visits couldn't be read and why. `netkeeper linkedin runs` and `netkeeper linkedin run <id>` show the same.

For Gmail, see the reason codes in [Gmail setup](../gmail-setup.md#troubleshooting).

Still stuck? Open an issue with a support bundle and screenshots: see [Reporting a problem](reporting-a-problem.md).

## Installing and opening the UI

| What you see | What to do |
|---|---|
| "The frontend is not built" | Run `make build-ui`, then reload. Run it again after each `git pull`. |
| A banner that says the UI requires Chrome | Open <http://127.0.0.1:8000> in Google Chrome. |
| "Backend unreachable" in the header | `serve` stopped. Start it again: `.venv/bin/netkeeper serve`. |
| `serve` says the address is already in use | Another `serve`, or another program, has port 8000. Stop it, or run `netkeeper serve --port 8080`. |

## The LinkedIn Chrome

| What you see | What to do |
|---|---|
| "Chrome was not reachable or went away mid-run" | Start the LinkedIn Chrome: `make chrome`, or the command `netkeeper browser launch` prints. Then `netkeeper preflight`. |
| Chrome ignores the debug port | Chrome 136 and later refuse it on your default profile. Use the command netkeeper prints, which has its own `--user-data-dir`. |
| Chrome won't start on the profile after a crash | `make chrome` clears the profile lock a crashed Chrome left behind. |
| "the browser was busy with another run" | One netkeeper process uses the browser at a time. `netkeeper preflight` names the process that holds it. Wait for that run, or stop it. |
| Posture: the activity lock "is probably stuck", or is held by something netkeeper can't name | Stop the process the warning names. netkeeper releases the lock when its holder exits. |

## Why a LinkedIn run stopped

netkeeper classifies every answer LinkedIn gives (spec [9.7](../architecture.md#97-failure-classification-and-heat)). These are the endings that need you; the rest, such as "read to the end of the connections list" or "made the visits it was allowed", are normal.

| The run says | What it means | What to do |
|---|---|---|
| LinkedIn throttled it | LinkedIn pushed back (HTTP 429 or 999). netkeeper raised heat and slowed down. | Nothing. Heat decays on its own, and runs resume. The **LinkedIn** page shows when. Use **Clear heat…** only if you know the block was something else. |
| LinkedIn showed a checkpoint | LinkedIn wants you to prove you're you. netkeeper set the session flag and stopped. It never retries a checkpoint. | In the LinkedIn Chrome, open LinkedIn yourself and resolve it. Then choose **Clear flag…** on the banner, or run `netkeeper linkedin clear-flag`. |
| LinkedIn asked to log in | The session expired or was logged out. netkeeper set the session flag. | Log in to LinkedIn in the LinkedIn Chrome, then run `netkeeper preflight`. It clears the flag once it sees a live session. |
| the page's answers changed shape | LinkedIn served a page netkeeper doesn't recognize. It gave up on that page for the run. | Run once more by hand. Two connections runs in a row ending this way trip the route-changed breaker (below). If it keeps happening, LinkedIn changed something; open an issue. |
| lost some of the page's answers | The page loaded, but Chrome didn't keep some answers' bodies. | Nothing, once. Repeated losses trip the answer-lost limit (below). |
| too many profiles were not found | Several profiles in a row were missing. None was recorded as not found. | Nothing. They're visited again later. |
| refused: the session is flagged | A checkpoint or login wall set the session flag earlier. | Clear it as above. |
| refused: heat is over its skip threshold | Heat is too high for browser jobs. | Wait. The **LinkedIn** page shows when runs resume. |
| outside active hours, or today's or this week's budget is spent | A limit you set. | Nothing, or change it on **Settings**, under **LinkedIn active hours** or **LinkedIn budgets**. |
| refused: scheduled runs are disarmed | The schedule is off. | **Arm scheduled runs** on the **LinkedIn** page, when you're ready. |
| refused: the first LinkedIn inbox poll is run by hand | Scheduled inbox polls wait for one you start. | **Check inbox now** on the **LinkedIn** page, or `netkeeper linkedin inbox`. |
| refused: the route-changed breaker, the answer-lost limit, the Contact info breaker, the inbox breaker, or the inbox owner breaker is tripped | Repeated failures of one kind stopped scheduled runs of that kind. Runs you start by hand still go. | Start one run of that kind by hand. One that reaches its end clears the breaker. Or, without a run, choose **Reset breaker** on the tripped breaker's row on **Settings**, under **Posture**, or run `netkeeper linkedin schedule reset-breaker`. |
| the page showed another LinkedIn mailbox than this account's | The LinkedIn Chrome is signed in to a different LinkedIn account. | Sign back in to yours. If you changed accounts on purpose, choose **Forget inbox owner** on **Settings**, under **Posture**, or run `netkeeper linkedin inbox-forget-owner`. Scheduled inbox polls then wait until you poll by hand (**Check inbox now** on the **LinkedIn** page, or `netkeeper linkedin inbox`) while Chrome is signed in to the new account; that poll records it. |
| refused: the inbox owner was forgotten, so the poll that records it again is run by hand | You chose **Forget inbox owner** (or ran `netkeeper linkedin inbox-forget-owner`), and no poll you started has recorded the mailbox since. | Check that Chrome is signed in to your LinkedIn account, then **Check inbox now** on the **LinkedIn** page, or `netkeeper linkedin inbox`. A poll that sees no conversation records no mailbox, so the hold stays; poll again once a conversation shows in your LinkedIn inbox. |
| the first poll could not read back to the earliest outreach | The first LinkedIn inbox poll couldn't reach your oldest watched outreach, so some older replies may be missed. | Check older LinkedIn replies by hand, then choose **Acknowledge** on **Settings**, under **Posture**, or run `netkeeper linkedin inbox-acknowledge`. |
| an error stopped it | Something unexpected went wrong. | Check `serve`'s log (`<data dir>/logs/serve.err.log` under launchd, or its terminal), then run once by hand. If it repeats, open an issue with the log lines. |
| the netkeeper process running it stopped | `serve` stopped or restarted mid-run. | Start the run again. An enrichment can **Resume** its plan once, from the run's detail. |

## Posture warnings

Each warning starts with the protection it belongs to. These are the ones you're most likely to see, quoted from the report:

| The warning says | What to do |
|---|---|
| "nothing has checked the LinkedIn session yet" | Run `netkeeper preflight`. |
| "the last check (…) found no LinkedIn session in this Chrome profile, so no job can run" | Log in to LinkedIn in the LinkedIn Chrome, then run `netkeeper preflight`. |
| "the session was last confirmed …, more than 24 h ago, so it may have expired since" | Run `netkeeper preflight` again. |
| "LinkedIn answered with a checkpoint on …" or "LinkedIn answered with a logged out on …" | The session flag is up. Clear it as in the table above. |
| "scheduled jobs: no job kind has a due time for this account, so nothing fires on its own" | Expected until `serve` has run with scheduled runs armed. |
| "the first LinkedIn inbox poll couldn't read back to …" | Check older LinkedIn replies by hand, then choose **Acknowledge** under **Posture**. |
| "LinkedIn auto-send is on (campaigns.linkedin_auto_send)" | You turned on LinkedIn auto-send. Unless you mean it, turn it off on the Settings page, or in `config.toml` when the file sets it (the page shows it locked then). Off applies at once. |
| "linkedin.pacing.… is …", or "linkedin.cdp_url points at …, not this machine's loopback" | A value in `config.toml` weakens a protection. Remove it to get the default back. |
| "the last complete full sync (run …) refused to age anyone" | A sync would have marked too many connections gone at once, so it marked none. Check the run it names. The warning clears after a later complete full sync. |

## Gmail

| What you see | What to do |
|---|---|
| "Gmail needs you to sign in again" | Choose **Re-authorize** on the banner, or run `netkeeper gmail login`. Email steps wait until you do. |
| **Arm to send** says "no netkeeper test draft was found in the Drafts of … at the last check" | Make a test draft from a campaign's review (**Send a test of step 1**) while the mailbox is armed for drafts, then **Check now**. The arm steps say "netkeeper found its own draft." when it's found. |
| A step's email went out as a draft | The mailbox is armed for drafts only. Send the draft yourself, or **Arm to send**. |

## Actions that stay on the command line

Everything else is in the UI. These remain in a terminal or a file, most of them by design:

| Action | How |
|---|---|
| Start `serve` at login | Settings, Run at login, or `netkeeper login-item on`. See [Run at login](../../README.md#run-at-login-macos). Two data directories each need their own `web.port`: the second `serve` can't bind a port the first holds, and its login item would restart it every 10 seconds. |
| Start the LinkedIn Chrome | `make chrome`, or the command `netkeeper browser launch` prints. netkeeper never starts a browser. |
| Check the LinkedIn Chrome (optional) | `netkeeper preflight`. |
| Turn on LinkedIn auto-send | Settings, **Campaign defaults**, **LinkedIn auto-send**, and confirm. It applies from the next auto-send check, within 10 minutes. `config.toml`'s `[campaigns] linkedin_auto_send` still works and wins over the page. Read [ADR 0008](../adr/0008-auto-send.md) first. |
| Import the old mailing tool's history (optional) | `netkeeper history import` and `netkeeper history scan-gmail`. See [the guide](../history-import.md). |
| Restore a backup | `netkeeper restore <name or path>`, with `serve` stopped. The **Backup files** card in Settings shows each backup's command. See [Backups and restore](backups.md). |
