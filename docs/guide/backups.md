# Backups and restore

Everything netkeeper knows lives in one SQLite file, `netkeeper.sqlite3`, in its data directory. A backup is a consistent copy of that file, taken without stopping `serve`, in the data directory's `backups/` folder, named by its UTC time (for example, `netkeeper-20261017T031500Z.sqlite3`).

A backup holds no secrets. Your Gmail token and OAuth client stay in the macOS Keychain, and the LinkedIn session stays in the LinkedIn Chrome's profile, so neither is in a backup or replaced by a restore.

## Nightly backups

With nightly backups on, `serve` backs up the database every night while it runs, and keeps the newest 14 by default. On **Settings**, under **Backups**, you turn the nightly backup on or off and set how many to keep.

## Back up now

Do this before an update or any change you might want to undo.

- On **Settings**, under **Backups**, choose **Back up now**. The panel lists every backup with its date and size.
- From a terminal: `netkeeper backup` (or `make backup`). `netkeeper backup list` lists the backups, newest first.

## Restore

Restore runs only from a terminal, because `serve` must be stopped first. The **Backups** panel shows the command.

1. Stop `serve`: press Ctrl+C in its terminal. If it runs at login, run `scripts/install-launchd.sh --uninstall` (install it again afterward).
2. Pick a backup from `netkeeper backup list`.
3. Restore it:

   ```sh
   netkeeper restore ~/Library/Application\ Support/netkeeper/backups/netkeeper-20261017T031500Z.sqlite3
   ```

   `restore` refuses while `serve` is running. It first backs up the current database, then puts the backup in its place, migrates it to the current version, and prints what it did.
4. Check it: `netkeeper db current` prints the revision the database is at and the newest one available; they match.
5. Start `serve` again.

To undo a restore, restore the backup it made of the database it replaced.
