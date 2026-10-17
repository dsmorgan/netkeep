# 0012. Chrome profiles: the UI never shares netkeeper's LinkedIn Chrome

Date: 2026-10-17

## Status

Proposed (#595).

## Context

netkeeper drives LinkedIn through one Chrome that runs a dedicated profile directory with a remote-debugging port (ADR 0002). The maintainer asked whether the UI should live in that same profile, "same, just different tabs", or in a separate one (#595). #549 and #599 wait on the answer. [Chrome profiles: research and mechanics](../design/chrome-profiles.md) has the evidence and sources. In short:

- **The port exposes the whole process.** Every profile in a profile directory runs in one Chrome process. Any local program that reaches the port can list every tab in every profile of that process, run script in any of them, and read their cookies, without a password.
- **The UI has no login.** It trusts requests from its own origin. Script inside a UI tab has that origin, so whoever holds the port can press any UI button, including the one that arms auto-send.
- **Runs act on tabs and focus.** A run opens a background tab and closes it at the end (#195). Prefill and auto-send bring their tab forward, and auto-send closes the sent bubble (#444, #516). Each would have to keep telling UI tabs apart. A tab brought forward raises its window, so if you work in that window, the run interrupts you (#488).
- **It has already gone wrong once.** During the CP8c capture, a UI tab in the same profile kept polling the API and polluted the capture (#520).
- **Your everyday Chrome has no port.** Chrome 136 and later ignore the port flag on the default profile directory. An opt-in at `chrome://inspect` (Chrome 144 and later) asks before each connection.

## Options

1. **Same profile, tabs.** One window holds LinkedIn and the UI. The UI sits behind the port, among the tabs runs act on, in the window runs raise.
2. **Same process, second profile.** This looks separate but isn't: the second profile runs in the same process, behind the same port.
3. **Separate, managed.** netkeeper starts its own LinkedIn Chrome on demand: the dedicated profile and port, with the command `netkeeper browser launch` prints today. The UI opens as an app window in your everyday Chrome, which has no port. You never manage either profile by hand.
4. **A third, portless profile that netkeeper owns for the UI.** As safe as option 3, with one more profile to manage. It's the fallback if option 3's app window proves unreliable.

## Decision

Option 3. The UI never opens in the LinkedIn Chrome, and netkeeper starts the LinkedIn Chrome itself, but only when you act.

1. **The UI opens only in a Chrome with no debugging port:** your everyday Chrome, as an app window. Without Chrome, it opens in the default browser, and #549's banner says Chrome is required. *Reason:* the port exposes everything in its process, and the UI has no login.
2. **The UI opener targets your everyday Chrome's profile directory, never whichever Chrome is running.** *Reason:* on macOS, the generic open command can send the URL to either running Chrome.
3. **netkeeper may start its own LinkedIn Chrome, and nothing else.** It uses the installed Chrome, the dedicated persistent profile, the configured port, and exactly the flags `browser launch` prints. It never runs headless, never uses a fresh profile, and never adds a flag. *Reason:* ADR 0002 forbids a second identity, not a launcher. When netkeeper starts the same profile, LinkedIn sees the same device, fingerprint, and cookie jar.
4. **Only you start it,** with a button (#599) or a start-at-login setting you turn on. A run never starts Chrome. When Chrome is down, the run fails and the scheduler retries later (ADR 0002). *Reason:* starting the browser stays your visible act, never a side effect of a run.
5. **netkeeper attaches only to the configured port, serving the dedicated profile.** *Reason:* it must never drive a Chrome you started for other purposes.
6. **You sign in to LinkedIn yourself,** in that window, the first time and whenever LinkedIn asks again. netkeeper never types credentials. *Reason:* the sign-in is the identity, and you own it.
7. **A UI tab in the LinkedIn Chrome is a warning.** Posture reports it, and the UI's banner asks you to close the tab. *Reason:* the warning keeps rule 1 true when someone opens the UI there by hand, as in CP8.

This ADR supersedes one clause of ADR 0002, "never launches a browser of its own", for rules 3 to 5 only. Attach-only for runs is unchanged. On acceptance, ADR 0002 gets a dated amendment, and the "Browser identity" line and `make chrome` note in `CLAUDE.md` change to match.

## Consequences

- **#549:** `serve`, or a `netkeeper open` command, opens the UI as an app window in your everyday Chrome.
- **#599:** **Start Chrome** and start-at-login launch the dedicated profile on its port. Posture and the LinkedIn page show whether that Chrome is running. The button is disabled while a run holds the browser. Together with #549, this closes the launch question.
- **First run:** setup offers **Start LinkedIn Chrome**, which opens LinkedIn's sign-in page. You sign in, and posture confirms the session. You don't paste a command or create a profile.
- **When that Chrome isn't running,** the top bar and the LinkedIn page say so and offer **Start Chrome**. Runs fail and retry, as today.
- **Focus:** a run raises only the LinkedIn Chrome's window. #488's live check still applies to that window.
- **Tab handling gets simpler:** runs' context holds no UI tabs. Runs keep their origin checks.
- **Cover traffic stays:** you can still browse LinkedIn yourself in that window (ADR 0002).
- **Keep true:** nothing else opens in the LinkedIn Chrome, the UI is never opened in a Chrome with a port, and no launch path adds a flag that `browser launch` doesn't print.
- **#594** decides how start-at-login is installed. This ADR decides only what it may start.
