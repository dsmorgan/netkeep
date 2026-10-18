# 0012. Chrome profiles: one netkeeper Chrome for the UI, LinkedIn, and setup

Date: 2026-10-17

## Status

Accepted (2026-10-17)

Revised 2026-10-17 after maintainer review (#595).

## Context

netkeeper drives LinkedIn through one Chrome that runs a dedicated profile directory with a remote-debugging port (ADR 0002). #595 asks whether the UI should live in that profile or a separate one. [Chrome profiles: research and mechanics](../design/chrome-profiles.md) has the evidence and the code checks. In short:

- **Your everyday Chrome can't be netkeeper's Chrome.** Chrome 136 and later ignore the port flag on the default user data directory (`--user-data-dir`), so no profile inside your everyday Chrome works either. "The same profile" means netkeeper's dedicated profile.
- **Setup needs tabs.** Gmail linkage opens Google Cloud Console pages in new tabs, then sends the UI's own tab to Google's consent page and back.
- **Runs act on tabs and focus** (#195, #444, #516). Today bringing forward and closing act only on the run's own tab, and the bubble check reads only LinkedIn's tabs. The gap: a run's background tab can land in whichever window Chrome picks, including the UI's.
- **The UI's traffic never reaches LinkedIn.** It goes to localhost, and runs watch network traffic only on their own tab. The #520 capture mixed in UI traffic because a netlog records the whole browser. That's the residual risk: close the UI before such a capture, as #520's setup already says.
- **The port adds little exposure.** Anything that reaches the port can already drive LinkedIn and read the profile's cookies. The UI's page, or a #618 per-install API secret held in it, adds little to that.

## Options

1. **One netkeeper Chrome (chosen):** the UI, LinkedIn, and setup tabs in the dedicated profile.
2. **Separate (rejected):** the UI in your everyday Chrome. You'd have to know which Chrome to look in, and Gmail linkage would open its tabs away from the UI.
3. **Same process, second profile (rejected):** it shares the process and the port, so it separates nothing and adds a profile to pick.
4. **A third, portless profile for the UI (rejected):** three places to look.

## Decision

Option 1. You look in one place: netkeeper's Chrome.

1. **netkeeper's Chrome holds the UI, LinkedIn, and setup.** It's the dedicated persistent profile on the configured port. *Reason:* one place to look, and setup's tabs open where the UI is.
2. **One launcher is the only spawn.** It starts the installed Chrome with exactly the flags `browser launch` prints, plus `--app=<the UI's URL>` and, from the first-run button only, LinkedIn's sign-in or feed URL as a positional argument. No headless mode, no fresh profile, no other flag or URL. Gmail linkage needs no launch URL, because the UI sends its own tab to Google. *Reason:* ADR 0002 forbids a second identity, not a launcher.
3. **The UI opens as an `--app` window in that Chrome.** *Reason:* an app window has no tab strip, so a run's tab never opens among the UI's tabs, and the UI's window never switches to another tab. A raised run window can still cover it.
4. **Runs touch only what they own.** A run's tab opens in its own window (new, #632). Bringing forward and closing act only on the run's own tab (today). Reads of other tabs keep to LinkedIn's origin (the bubble check today; the tab wait, #634). The cookie read asks only for LinkedIn's cookies (new, #633). *Reason:* the UI and setup tabs share the process.
5. **Only you trigger a spawn:** a button, `netkeeper open`, or a start-at-login setting you turn on. A run never spawns anything; when netkeeper's Chrome is down, it fails and retries later (ADR 0002). *Reason:* starting a browser stays your visible act.
6. **netkeeper attaches only to the configured port, serving the dedicated profile** (#624). *Reason:* it must never drive a Chrome you started for other purposes.
7. **You sign in yourself,** to LinkedIn and to Google. *Reason:* the sign-in is the identity, and you own it.
8. **The UI works in any browser, with no "open in netkeeper's Chrome" hint.** *Reason:* only a port read could tell which browser holds the page, and opening it elsewhere risks nothing.

This ADR supersedes ADR 0002's "never launches a browser of its own" for rules 2, 3, and 5 only. Attach-only for runs is unchanged.

**Accepted near-term:** your own clicks in a run's tab can still disturb that run. The end state is a bundled or custom browser that netkeeper controls: focus, hidden run tabs, and no navigation by you during a run (Phase 7, #631).

On acceptance, ADR 0002 gets a dated amendment, `CLAUDE.md`'s "Browser identity" line and `make chrome` note change, and `tests/test_browser_safety.py` allows one spawning function in one module, pinned to the printed command. Playwright's launch calls stay banned. The design doc lists the rest.

## Consequences

- **#549:** `netkeeper open` and an open-at-login setting open the UI's app window in netkeeper's Chrome, starting it if it's down.
- **#599:** **Start Chrome** and start-at-login run the launcher.
- **#636:** the launcher itself: the hand-off to a running Chrome, its refusals, and the scanner's one exception.
- **#624:** attach refuses any Chrome that isn't the dedicated profile, which keeps netkeeper away from your everyday Chrome.
- **First run:** setup's **Start netkeeper's Chrome** opens the UI and LinkedIn's sign-in page. You sign in, posture confirms the session, and Gmail linkage happens in the same Chrome.
- **Focus:** a run raises its own window, not the UI's (#632). #488's live check still applies.
- **The port reaches more:** the UI's page and, after Gmail linkage, your Google session. That's acceptable because the port already gives full control of LinkedIn. #624 and the loopback-only port stay the guard.
- **Keep true:** runs act only on their own tabs, nothing reads other sites' tabs or cookies, and no spawn grows an argument these rules don't allow.
