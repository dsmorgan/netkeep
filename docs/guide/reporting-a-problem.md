# Reporting a problem

When something goes wrong, open an issue with what you did, what you expected, and what happened instead. Attach a **support bundle** and screenshots of what you saw. The bundle shows the person helping you your versions, settings and recent runs without you copying them by hand.

netkeeper never uploads a bundle. You download it and attach or send it yourself.

## Make a support bundle

- On **Settings**, under **Support**, choose a level, check what it includes and leaves out, and choose **Download**.
- From a terminal, run `netkeeper support bundle`. It writes `netkeeper-support-standard-<time>.zip` in the current folder. Use `-o <file>.zip` for another name and `--since 3d` (hours, days or weeks) for a shorter or longer window. It never overwrites a file.

Making a bundle only reads. It never attaches to the LinkedIn Chrome: posture comes from what netkeeper last recorded, as on the Settings page.

## Levels

| Level | Holds | Send it |
|---|---|---|
| **Standard** (the default) | Versions (netkeeper, Python, macOS, and Chrome's version from the installed app), settings and where each comes from, posture, breakers, the LinkedIn schedule, recent runs (counts, stop reasons, timings), the migration revision, and the last 7 days of logs. Names, emails, phone numbers, web addresses, LinkedIn ids and slugs, message text, your user name and run notes are replaced by placeholders such as `<personal-17>` or `<email-3>`. | Attach it to a public issue. |
| **Detailed** | The same, redacted the same way, with 30 days of logs, more runs, and `redactions.json` (how many values each placeholder type stands for). | Attach it to a public issue. |
| **Full** | The same with real values: names, emails, LinkedIn URLs and ids, and run notes. You type `full` (terminal) or tick a box (Settings) first. | Privately, only when someone helping you asks for it. Never attach it to a public issue. |

**No level ever includes a secret.** The bundle reads a fixed list of fields and the log file, nothing else: never your Gmail OAuth client or token, a cookie, a Keychain value, or a CSRF token. Anything in the log that looks like a credential is replaced with `<secret>` at every level, the full one included.

The same value has the same placeholder in every file of one bundle, so a reader can follow one contact through a run without knowing who it is. A placeholder can't be turned back into its value.

Detailed is meant to hold page-structure captures from failing runs. netkeeper doesn't store any yet, and the bundle's `README.txt` says so.

## What's in the file

Every bundle has a `README.txt` (the level, what's inside, how to send it) and a `manifest.json` (each file with its size and checksum, the level, when it was made, and the netkeeper version). The rest:

- `versions.json`, `settings.json`, `posture.txt`, `breakers.json`, `jobs.json`, `runs.json`, `database.json`
- `logs/netkeeper.log`: the log file's records for the chosen days, oldest first, across the rotated files. A long log keeps its newest records.

Open the `.zip` and read it before you send it, if you like. Everything in it is plain text.
