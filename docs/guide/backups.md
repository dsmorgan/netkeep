# Backups and restore

Everything netkeeper knows lives in one SQLite file, `netkeeper.sqlite3`, in its data directory. A backup is a consistent copy of that file, taken without stopping `serve`, in the data directory's `backups/` folder. Each backup is named by its UTC time, for example `netkeeper-20261017T031500Z.sqlite3`.

A backup holds no secrets. Your Gmail token and OAuth client stay in the macOS Keychain, and the LinkedIn session stays in the LinkedIn Chrome's profile. A backup doesn't contain them, and a restore doesn't replace them.

## Nightly backups

**Nightly backups** is on by default. While `serve` runs, it checks every 10 minutes and backs up once a night, from 03:00 in your LinkedIn timezone. If your Mac is asleep at 03:00, the backup runs within 10 minutes of waking. A backup you make by hand after 03:00 counts as that night's.

Each backup, nightly or by hand, deletes the oldest backups beyond **Backups to keep**, which is 14 by default. You set both on **Settings**, under **Backups**. A change applies at `serve`'s next check, without a restart.

## Back up now

Back up before an update, or before any change you might want to undo.

- On **Settings**, under **Backup files**, choose **Back up now**. The card lists every backup, newest first, with its date and size.
- From a terminal, run `netkeeper backup` (or `make backup`). `netkeeper backup list` lists the backups, newest first.

## Restore

You restore only from a terminal, because `serve` must be stopped first. On the **Backup files** card, each backup has a **Restore with** command to copy. The command uses the backup's full path, and starts with `NETKEEPER_DATA=` when `serve` uses another data directory.

1. Stop `serve`.
   - If you started it in a terminal, press Ctrl+C there.
   - If it runs at login, stop the LaunchAgent: `launchctl bootout gui/$UID/fun.tnkr.netkeeper`. Stopping the process alone isn't enough, because launchd restarts it.
2. Pick a backup from the **Backup files** card or from `netkeeper backup list`.
3. Restore it. Paste the card's command, or give the backup's name as `backup list` prints it:

   ```sh
   netkeeper restore netkeeper-20261017T031500Z.sqlite3
   ```

   If you use another data directory, put `NETKEEPER_DATA=<your data directory>` in front, as the card's command does.

   `restore` refuses while `serve` runs. It also refuses while any other process has the database open, with "another netkeeper process has the database open; stop it and retry". In both cases it changes nothing. Otherwise, it:
   1. Backs up the database it's about to replace, as `netkeeper-<time>-pre-restore.sqlite3`.
   2. Writes the backup over the database in one transaction. If the write fails, the old database stays as it was.
   3. Runs `db upgrade`, then prints what it did and the command that undoes it.
4. Check it. `netkeeper db current` prints `current:` and `head:`, and the two revisions match.
5. Start `serve` again.
   - In a terminal, run `.venv/bin/netkeeper serve`.
   - For the LaunchAgent, run `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/fun.tnkr.netkeeper.plist`, or run `scripts/install-launchd.sh` again with the options you used before.

`db upgrade` also refuses while `serve` runs, so a restore and an upgrade never overlap.

## Undo a restore

Restore the pre-restore backup. `restore` prints the exact command, for example `netkeeper restore netkeeper-20261017T145000Z-pre-restore.sqlite3`. The card marks this backup "taken before a restore: restoring it undoes that one". It doesn't count toward **Backups to keep**, and netkeeper keeps the newest one for 30 days, or until your next restore makes a new one.
