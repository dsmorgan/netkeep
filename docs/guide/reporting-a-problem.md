# Reporting a problem

When something goes wrong, open an issue with what you did, what you expected, and what happened instead, with screenshots of what you saw. Then make a **support bundle**: it shows the person helping you your versions, settings and recent runs without you copying them by hand.

netkeeper never uploads a bundle. You download it and send it yourself. Send it privately, by email to the person helping you, and say in the issue that you have one. Attach a standard or detailed bundle to a public issue only after you've skimmed its `logs/netkeeper.log` (see [the known gap](#the-known-gap)).

## Make a support bundle

- On **Settings**, under **Support**, choose a level, check what it includes and leaves out, and choose **Download**.
- From a terminal, run `netkeeper support bundle`. It writes `netkeeper-support-standard-<time>.zip` in the current folder. Use `-o <file>.zip` for another name and `--since 3d` (hours, days or weeks) for a shorter or longer window. It never overwrites a file.

The download starts only from netkeeper's own Settings page (or the address bar), never from another site's page. Making a bundle only reads. It never attaches to the LinkedIn Chrome: posture comes from what netkeeper last recorded, as on the Settings page.

## Levels

| Level | Holds | Send it |
|---|---|---|
| **Standard** (the default) | Versions (netkeeper, Python, macOS, and Chrome's version from the installed app), settings and where each comes from, posture, breakers, the LinkedIn schedule, recent runs (counts, stop reasons, timings), the migration revision, and the last 7 days of logs. Names, emails, phone numbers, web addresses, LinkedIn ids and slugs, message text, your user name and run notes are replaced by placeholders such as `<personal-17>` or `<email-3>`. | Privately, by email. On a public issue only after you've skimmed the log. |
| **Detailed** | The same, redacted the same way, with 30 days of logs, more runs, and `redactions.json` (how many values each placeholder type stands for). | Privately, by email. On a public issue only after you've skimmed the log. |
| **Full** | The same with real values: names, emails, LinkedIn URLs and ids, and run notes. You type `full` (terminal) or tick a box (Settings) first. | Privately, only when someone helping you asks for it. Never attach it to a public issue. |

**No level ever includes a secret.** The bundle reads a fixed list of fields and the log file, nothing else: never your Gmail OAuth client or token, a cookie, a Keychain value, or a CSRF token. Anything in the log that looks like a credential is replaced with `<secret>` at every level, the full one included.

The same value has the same placeholder in every file of one bundle, so a reader can follow one contact through a run without knowing who it is. A placeholder can't be turned back into its value.

Below the full level, the log keeps only each record's first line: a later line of a message, and a traceback's exception messages, are replaced by `<message left out>`. A traceback keeps its frames (file, line, function, source line) and each exception's type. A run's error keeps only the exception's type.

Detailed is meant to hold page-structure captures from failing runs. netkeeper doesn't store any yet, and the bundle's `README.txt` says so.

## What's in the file

Every bundle has a `README.txt` (the level, what's inside, how to send it) and a `manifest.json` (each file with its size and checksum, the level, when it was made, and the netkeeper version). The rest:

- `versions.json`, `settings.json`, `posture.txt`, `breakers.json`, `jobs.json`, `runs.json`, `database.json`
- `logs/netkeeper.log`: the log file's records for the chosen days, oldest first, across the rotated files. A long log keeps its newest records.

Everything in it is plain text. Open the `.zip` and read it before you send it.

## The known gap

Redaction replaces every value the code marks as personal, and then everything that looks like an email, a phone number, a web address, or a LinkedIn id. A name the code logged without marking it, in the first line of a log message, matches none of those, so it can still appear in `logs/netkeeper.log`. Skim that file before you share a standard or detailed bundle anywhere public, and delete the bundle or send it privately if you find one.
