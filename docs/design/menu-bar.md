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

- **The daily check (#762):** `serve` asks for the newest published GitHub release (`update.latest_release`, the request `netkeep update --check` makes), at most once a day, while **Check for updates daily** is on (`updates.check_daily`, default on). It stores the answer and when it checked in `update/last-check.json` in the data directory, and `GET /update-check` serves it. **Update to X.Y.Z** appears only when that answer is newer than the running version. The source is GitHub, not PyPI: a release is published only after PyPI has the version and its `constraints.txt` and `SHA256SUMS` are attached (ADR 0011, rule 9). A failed check logs one line and waits a day.
- The menu bar imports everything it uses at startup, before any update can replace the tool environment.
- It starts `netkeep update` in its own session, so the update outlives the menu bar, and it watches the update's state file (`state.json` in the data directory's update folder, `netkeep/services/update.py`). It shows each phase and any refusal.
- `update --finish` restarts the menu-bar job last, after its health check, so the menu bar runs the new code.

## Jobs, the preference, and Quit

- **Labels:** the menu-bar job's label is the login item's label (`fun.tnkr.netkeep`, or `fun.tnkr.netkeep.<hash>` for another data directory) plus a fixed `.menubar` suffix. It runs the helper bundle's script. `KeepAlive` uses `SuccessfulExit: false`, so launchd restarts the menu bar after a crash but not after Quit.
- **The preference:** **Show menu-bar icon** is stored like the other Settings values, in `settings_kv`, default on. The plist alone can't hold it, because Run at login off removes both plists. It's `menubar.show_icon` (#763): `config.toml` wins as for every value, only the local owner's value counts, and the Settings page shows it in the Run at login card, on macOS only. `setup`, `update`, and the Run at login switch read it with `ui_settings.show_menubar_icon`, which needs no request and creates no database.

| Action | Preference on | Preference off |
|---|---|---|
| Run at login on, or `netkeep setup` | Adds both jobs | Adds the `serve` job; removes the menu-bar job |
| Run at login off, or `setup --remove` | Removes both | Removes both |
| `update --finish` | Restarts both | Starts `serve` only |
| Turning the preference on or off | Adds or removes the menu-bar job, if Run at login is on | |

- **Quit:** `LoginItem.stop()` boots out the `serve` job and leaves its plist. Then the menu bar exits with status 0. Both come back at the next login.
- **The launcher (#754):** for each job whose plist exists, it bootstraps the job if `launchctl print` doesn't find it, then kickstarts it. After that it waits for `/health` and opens the window. So **Open netkeep** brings both processes back after a Quit.

## Checked on a real Mac (#760)

macOS 26.6.2 on Apple silicon, 2026-10-25, with a throwaway PyObjC prototype against a scratch data directory and Keychain service. Intel wasn't checked.

| Fact | Result | What follows |
|---|---|---|
| Survives `uv tool install` | It kept running for 10 minutes after a `--reinstall`. A shell-out ran the new version, and a late import worked. | Rule 6 stands. The menu still shows the version that started until `update --finish` restarts it. |
| Its own identity | A script that `exec`s Python, or a symlink to Python, shows as "python3.12" in **System Settings > Menu Bar**, and each Python version adds its own entry. A copy of Python, named for the helper and signed with its bundle id, shows as the helper. | The helper's executable is that copy (ADR 0014 amendments). |
| Keychain | The copy asks once on its first read, naming the helper. **Always Allow** holds across restarts. Rewriting the copy, even from the same Python, asks again. | Rule 12: rewrite the copy only when the installed Python changed. |
| Hidden item | The setting lists the unsigned helper and hides it. The process isn't told: `isVisible` stays true and no KVO change arrives. | The preference's help text says hiding it in System Settings leaves it running. |
| Notifications | From the script or the symlink: the call hangs or is refused (`UNErrorDomain` 1). From the signed copy: one notification arrived with the helper's name and icon. | Possible after 1.0 by an amendment (rule 10). |
| Gatekeeper | No `com.apple.quarantine`, before and after a rewrite in place. `spctl` rejects it ("no usable signature"), but launchd and a double-click start it without a dialog. | No change. |

### The helper bundle

```
netkeep.app/Contents/Helpers/netkeep Menu Bar.app/
  Contents/Info.plist         CFBundleIdentifier <launcher id>.menubar, LSUIElement, CFBundleIconFile
  Contents/MacOS/netkeep      a copy of the installed tool's Python, ad-hoc signed with -i <bundle id>
  Contents/lib -> <that Python's home>/lib   (@executable_path/../lib and the stdlib search both use it)
  Contents/Resources/netkeep.icns
```

The launchd job runs `Contents/MacOS/netkeep -c "<add the tool's site-packages>; run netkeep menubar"`, because a bundle executable can't carry arguments. Finder can't start the helper by itself, so **Open netkeep** and the launcher start it through `launchctl kickstart`.

**Open:** System Settings shows the helper's name without an icon, even after `lsregister -f`. The notification shows the icon, so the `.icns` is fine. #766 checks it with the bundle in `~/Applications`.
