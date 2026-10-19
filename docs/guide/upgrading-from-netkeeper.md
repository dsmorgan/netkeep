# Upgrading from netkeeper

netkeep was called netkeeper until October 2026 (#661). The name changed everywhere on your Mac too: the command, the data directory, the Keychain items, the login item and the browser's cookie. The first time the new version opens your data, it moves them once. This page says what moves, how to upgrade safely, and how to undo it.

The GitHub repository is now `github.com/dsmorgan/netkeep`, and GitHub redirects the old address, so `git pull` in your checkout keeps working. The checkout's folder can stay `netkeeper`.

## What happens

| What | Before | After | How |
|---|---|---|---|
| The command | `netkeeper` | `netkeep` | `netkeeper` still works for one release. It prints a one-line note, then runs `netkeep`. |
| The data directory | `~/Library/Application Support/netkeeper/` | `~/Library/Application Support/netkeep/` | Moved: one rename, nothing copied. The database file inside keeps its name, `netkeeper.sqlite3`. |
| Keychain items | service `netkeeper` (or `netkeeper@<hash>`) | service `netkeep` (or `netkeep@<hash>`) | Copied. The old items stay until you delete them. |
| The login item | `fun.tnkr.netkeeper` | `fun.tnkr.netkeep` | Replaced. On stays on, starting at your next login; off stays off. The old plist is removed. |
| Environment variables | `NETKEEPER_*` | `NETKEEP_*` | The old names still work for one release, with a warning. The new name wins when both are set. |
| The browser's cookie | `netkeeper_session_<port>` | `netkeep_session_<port>` | The old one still connects for one release, and the UI sets the new one beside it. |
| The UI's saved choices | `netkeeper.*` in the browser | `netkeep.*` | Moved the first time the UI loads: the theme, the dashboard tab, the contacts columns, the Chrome banner. |
| Log file | `logs/netkeeper.log` | `logs/netkeep.log` | New records go to the new file. Old files stay in `logs/`. |
| Backups | `backups/netkeeper-<time>.sqlite3` | `backups/netkeep-<time>.sqlite3` | Old backups still list, restore and prune. |

Only the default data directory moves, and only the default one migrates by itself. A `NETKEEP_DATA` that names the old or the new default folder, however it's spelled, counts as the default. If you set `NETKEEP_DATA` (or `NETKEEPER_DATA`) to another folder, it stays where it is, and nothing happens until you run `netkeep migrate` with that variable set: then its Keychain items and login item move to the new names.

The new login item keeps the old one's settings: a `NETKEEPER_CONFIG` or `NETKEEPER_KEYCHAIN_SERVICE` in the old plist becomes `NETKEEP_CONFIG` or `NETKEEP_KEYCHAIN_SERVICE` in the new one, with a path inside the old folder pointed at the new folder. A config in your checkout stays the one it reads.

A UI tab you left open keeps working too: it still sends the old request header, which netkeep accepts for one release. Reload it when you can.

When it's done, netkeep writes `migrated-from-netkeeper.json` in the data directory, recording what it did. With that file there, it doesn't run again.

### When it refuses

netkeep moves nothing, and says what to do, when:

- **Both folders exist.** netkeep can't tell which one is yours. Move the one you don't use somewhere else, then run `netkeep migrate`.
- **netkeeper is still running.** A `serve`, or a LinkedIn run, holds a lock in the old folder. Stop it, then try again.
- **netkeeper's Chrome is open.** It's using the profile inside the old folder. Quit that Chrome, then try again.
- **`NETKEEP_DATABASE_URL` or `NETKEEP_CONFIG` points inside the old folder.** Point it at the new folder first.
- **The rename itself fails,** for example because the folder is on another volume. Nothing moves; the message says why.

If the old folder comes back after a migration, netkeep keeps running on the new folder and warns that the old one is ignored. `netkeep migrate` explains it: check what's in the old folder and move it aside; don't move the new one. `reset-data.sh` and `chrome.sh` never recreate it, and never use the new folder before the move.

The migration holds the serve lock from its checks to its last step, so no `serve` starts on either folder halfway through.

Until the move succeeds, every command but `netkeep migrate`, `version` and `openapi` stops with the same message, so nothing starts on a new, empty data directory beside your real one.

## Upgrade

1. **Stop netkeeper.** Press Ctrl+C in the terminal running `serve`, and quit netkeeper's Chrome. Don't start Chrome again (`make chrome`, **Start Chrome**, `netkeep open`) until the migration is done: its profile is inside the folder that moves. If the login item runs it, stop it without removing it, so the migration keeps it on:

   ```sh
   launchctl bootout gui/$(id -u)/fun.tnkr.netkeeper
   ```

2. **Back up.** Copy the whole data folder, which holds the database, its backups, the logs and the Chrome profile:

   ```sh
   ditto ~/Library/Application\ Support/netkeeper ~/netkeeper-backup-$(date +%Y%m%d)
   ```

   The Keychain items need no backup: the migration only copies them.

3. **Update.** In your checkout: `git pull`, `make install`, `make build-ui`.

4. **See the plan.** It prints each step, and changes nothing:

   ```sh
   .venv/bin/netkeep migrate --dry-run
   ```

5. **Migrate,** with `.venv/bin/netkeep migrate`, or by starting `.venv/bin/netkeep serve`: the first start migrates the same way and prints what it did. The new login item starts at your next login; to start it now instead of running `serve` yourself, run `.venv/bin/netkeep login-item on`.

   If you skipped step 1's `launchctl bootout` and the old login item starts the new version, it migrates, starts the new login item and stops itself.

6. **Open the UI.** Your browser stays connected. If it asks you to connect, run `netkeep open`.

7. **Rename your variables.** If your shell profile or scripts set `NETKEEPER_DATA` or another `NETKEEPER_*` variable, rename it to `NETKEEP_*`.

8. **Later, clean up.** Once netkeep works, you can delete the old Keychain items: open Keychain Access, search for `netkeeper`, and delete the items whose name is `netkeeper` or starts with `netkeeper@`. Delete your backup copy when you no longer want it.

## Undo

To go back to netkeeper:

1. Stop netkeep (Ctrl+C, and quit its Chrome), and remove its login item, if it has one:

   ```sh
   launchctl bootout gui/$(id -u)/fun.tnkr.netkeep
   rm ~/Library/LaunchAgents/fun.tnkr.netkeep.plist
   ```

2. Move the folder back, and remove the marker:

   ```sh
   mv ~/Library/Application\ Support/netkeep ~/Library/Application\ Support/netkeeper
   rm ~/Library/Application\ Support/netkeeper/migrated-from-netkeeper.json
   ```

   Or restore your backup copy in its place, and then move the new folder aside (or delete it), so the next upgrade finds only one:

   ```sh
   mv ~/Library/Application\ Support/netkeep ~/netkeep-set-aside-$(date +%Y%m%d)
   ```

   If both folders hold a database, netkeep refuses to start rather than pick one.

3. Check out the version before the rename, then `make install` and `make build-ui`.

4. Turn its login item back on with `netkeeper login-item on`, if you used it.

The old Keychain items are still there, so the old version finds its secrets. Anything you changed in netkeep's Gmail sign-in after the upgrade is only under the new names.
