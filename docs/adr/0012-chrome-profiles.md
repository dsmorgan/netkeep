# 0012. Chrome profiles: the UI never shares netkeeper's LinkedIn Chrome

Date: 2026-10-17

## Status

Proposed (#595).

## Context

netkeeper drives LinkedIn through one Chrome that runs a dedicated profile directory with a remote-debugging port (ADR 0002). The maintainer asked whether the UI should live in that same profile, "same, just different tabs", or in a separate one (#595). #549 and #599 wait on the answer. [Chrome profiles: research and mechanics](../design/chrome-profiles.md) has the evidence. In short:

- **Runs act on tabs and focus.** A run opens a background tab and closes it at the end (#195). Prefill and auto-send bring their tab forward, and auto-send closes the sent bubble (#444, #516). The bubble check reads the other LinkedIn tabs. Each would have to keep telling UI tabs apart. A tab brought forward raises its window, so if you work in that window, the run interrupts you (#488).
- **A UI tab pollutes the profile it's in.** During the CP8c capture, a UI tab open in the test-account capture profile kept polling the API and mixed into the capture (#520). In the LinkedIn Chrome, the same tab would mix into the context that every run reads and that LinkedIn sees.
- **The port adds no new exposure for the UI.** Any local program can already call the API without a browser, so the port isn't the reason to keep the UI out. It does make everything in that process driveable, so the process should hold LinkedIn alone.
- **One process per profile directory.** Profiles inside one directory share a process and, as Chrome is known to behave (not verified live), its port. A second profile isn't separate.
- **Your everyday Chrome has no port.** Chrome 136 and later ignore the port flag on the default profile directory. An opt-in at `chrome://inspect` (Chrome 144 and later) asks before each connection.

## Options

1. **Same profile, tabs:** UI tabs among the tabs runs act on, in the window runs raise.
2. **Same process, second profile:** the same process and port, so not separate.
3. **Separate, managed:** netkeeper starts its LinkedIn Chrome when you ask; the UI opens as an app window in your everyday Chrome.
4. **A portless profile that netkeeper owns for the UI:** one more profile to manage. It's the fallback if option 3's app window proves unreliable.

## Decision

Option 3. The UI never opens in the LinkedIn Chrome, and netkeeper may spawn exactly two Chrome commands, only when you act.

1. **The UI opens only as an app window in your everyday Chrome,** or in the default browser without Chrome, where #549's banner says Chrome is required. *Reason:* it keeps the LinkedIn context to runs and your own LinkedIn browsing, and keeps runs' tab and focus changes out of the window you work in.
2. **The UI opener runs Chrome with only `--app=<the UI's URL>`, on your everyday profile directory, never by "whichever Chrome is running".** It may start that Chrome if it's stopped. *Reason:* on macOS, the generic open command can send the URL to either running Chrome.
3. **The LinkedIn launcher is the only spawn that may start a Chrome with a port.** It starts the installed Chrome on the dedicated persistent profile and the configured port, with exactly the flags `browser launch` prints. It never runs headless, never uses a fresh profile, and never adds a flag. *Reason:* ADR 0002 forbids a second identity, not a launcher, and the same profile is the same device, fingerprint, and cookie jar.
4. **One start URL is allowed:** LinkedIn's own sign-in or feed URL, as a positional argument, never a flag, and only from the first-run button. *Reason:* the first run needs a sign-in page, and a URL changes nothing LinkedIn can measure about the browser.
5. **Only you trigger a spawn:** a button, `netkeeper open`, or a start-at-login or open-at-login setting you turn on. A run never spawns anything. When the LinkedIn Chrome is down, the run fails and the scheduler retries later (ADR 0002). *Reason:* starting a browser stays your visible act.
6. **netkeeper attaches only to the configured port, serving the dedicated profile.** *Reason:* it must never drive a Chrome you started for other purposes.
7. **You sign in to LinkedIn yourself.** netkeeper never types credentials. *Reason:* the sign-in is the identity, and you own it.
8. **A UI tab in the LinkedIn Chrome is a posture warning,** and the UI's banner asks you to close it. *Reason:* it keeps rule 1 true when someone opens the UI there by hand.

This ADR supersedes ADR 0002's "never launches a browser of its own" for rules 2 to 5 only. Attach-only for runs is unchanged. On acceptance:

- ADR 0002 gets a dated amendment.
- `CLAUDE.md`'s "Browser identity" line and `make chrome` note change.
- `tests/test_browser_safety.py`'s ban on spawning a process gets one exception: one module, imported only by the CLI and `serve`'s wiring and never by `web/`, with one function whose two argument lists are built from config and pinned by tests to the lines netkeeper prints. Playwright's launch calls stay banned everywhere.

The design doc lists the other text that changes.

## Consequences

- **#549:** `netkeeper open` and an open-at-login setting open the UI as an app window.
- **#599:** **Start Chrome** and start-at-login launch the dedicated profile on its port. Posture and the LinkedIn page show whether it's running. The button is disabled while a run holds the browser.
- **First run:** setup offers **Start LinkedIn Chrome**, which opens LinkedIn's sign-in page (rule 4). You sign in, and posture confirms the session.
- **When that Chrome isn't running,** the top bar and the LinkedIn page say so and offer **Start Chrome**. Runs fail and retry, as today.
- **Focus:** a run raises only the LinkedIn Chrome's window. #488's live check still applies.
- **Keep true:** nothing else opens in the LinkedIn Chrome, the UI never opens in a Chrome with a port, and no spawn grows an argument these rules don't allow.
