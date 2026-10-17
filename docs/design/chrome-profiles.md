# Chrome profiles: research and mechanics

This document holds the evidence and the mechanics behind [ADR 0012](../adr/0012-chrome-profiles.md). The ADR holds the decision: one netkeeper Chrome, on the dedicated profile with the debugging port, holds the UI, LinkedIn, and setup steps such as Gmail linkage. Researched 2026-10-17 for #595, and revised the same day after the maintainer's review.

## How netkeeper attaches today

- `linkedin.cdp_url` (default `http://127.0.0.1:9222`) names the port. `AttachProvider` in `netkeeper/linkedin/browser.py` connects with Playwright's `connect_over_cdp` and reuses `browser.contexts[0]`. It never creates a context, and it never closes the browser or the context. It logs a warning when the attached Chrome has more than one context.
- The profile is `<data dir>/chrome-profile` (`CHROME_PROFILE_DIRNAME`). `netkeeper browser launch` and `GET /linkedin/browser` print the command from `chrome_launch_command` in `netkeeper/services/browser_launch.py`: `--remote-debugging-port=<port>` and `--user-data-dir=<profile>`, and nothing else. `scripts/chrome.sh` (`make chrome`) runs that command for a person.
- Nothing checks yet that the Chrome on the port is the dedicated profile. #624 adds that check at attach.

## Why not your everyday Chrome

Chrome 136 (2025) and later ignore `--remote-debugging-port` and `--remote-debugging-pipe` when Chrome runs on the default user data directory. The flags work only with a non-default `--user-data-dir`, which uses its own encryption key. Chrome made this change because attackers used remote debugging to steal cookies after App-Bound Encryption shipped. Source: [Changes to remote debugging switches to improve security](https://developer.chrome.com/blog/remote-debugging-port).

Chrome 144 added an opt-in at `chrome://inspect/#remote-debugging` that lets a tool connect to your running browser. Chrome asks you to accept each session and shows a "controlled by automated test software" banner while one is active. Sources: [Let your coding agent debug your browser session](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session) and [Auto-connect](https://developer.chrome.com/docs/devtools/agents/use-cases/auto-connect). netkeeper can't run unattended that way, and #624 refuses such a Chrome.

A second profile inside your everyday Chrome's data directory (`--profile-directory`) is still on the default user data directory, so it doesn't work either. So "the same profile" in ADR 0012 means netkeeper's dedicated profile. It's a second Chrome window with its own icon in the window list, and it is the one place you look when you operate netkeeper.

## How Gmail linkage works today

- **Google Cloud setup.** The Settings page's Gmail setup wizard (`frontend/src/features/settings/gmail-setup-wizard.tsx`) links to Google Cloud Console pages (new project, Gmail API, Branding, Audience, Create OAuth client). Each link opens a new tab (`target="_blank"`).
- **Authorization, from the UI.** `POST /mailboxes/oauth/start` (`netkeeper/web/api/mailboxes.py`) returns Google's authorization URL, built by `gmail_oauth.begin` with PKCE and a loopback redirect to the API's own `/mailboxes/oauth/callback`. The UI then sends its own tab to that URL (`navigation.assign` in `frontend/src/features/mailboxes/api.ts`). After you allow access, Google redirects that tab to the callback, which exchanges the code and redirects back to the Settings page.
- **Authorization, from the CLI.** The CLI starts its own loopback listener and prints the URL for you to open (`_present_authorization_url` in `netkeeper/cli.py`). It never opens a browser.

So the launcher never needs Google's consent URL. In netkeeper's Chrome, the wizard's Console tabs and the consent page open in the same Chrome as the UI, and the Google sign-in you make there stays in that profile.

## What runs do with tabs, and what changes

| Path | Today | With the UI in the same Chrome |
| --- | --- | --- |
| Open | `_open_tab` sends `Target.createTarget` with `url: about:blank` and `background: true`, and no `newWindow` (#195, PR #468). On failure it falls back to `context.new_page()`, a foreground tab, and logs an "opened in front" note. | Chrome places a background tab in a window it picks, which can be the UI's or a setup window. **New (#632):** open the run's tab in its own window, likely `newWindow: true` with `background: true`. Check placement and focus live first. |
| Wait for the new tab | `_open_tab` opens a page-level session on every page that appeared since it asked, to read its target id (`Target.getTargetInfo`). | A UI or Google tab you open during that wait gets a session too. It's read-only and harmless, but it isn't the run's tab. **New (#634):** skip pages that aren't `about:blank`, or match without attaching. |
| Bring forward | `bring_tab_forward` calls `bring_to_front` once, on the run's own page, before the Message click (ADR 0007, ADR 0008). | Unchanged. With #632, it raises the run's window, not the UI's. |
| Close | The run closes only its own page, except a prefill's handed-over tab. Auto-send closes the sent bubble inside the run's own tab (#516, `_close_by_rule`). | Unchanged. |
| Read other tabs | `bubble_check` (`message-check --bubble`, #495) lists `context.pages` and reads only the tabs on the LinkedIn origin it's given. | Unchanged. **New (#634):** a test pins every `context.pages` use in the package to an ownership or LinkedIn-origin filter. |
| Network | The body tap (`_open_body_tap`), the package's only `Network.enable`, is a page-level session on the run's own tab. | Unchanged. The UI's traffic isn't seen. |
| Cookies | `_read_session_cookies` in `netkeeper/linkedin/preflight.py` calls `context.cookies()` with no URLs, then keeps LinkedIn's two session cookies by name and domain. It copies names and expiry times only. | After Gmail linkage, the jar holds your Google cookies too. **New (#633):** pass LinkedIn's URLs so the read never returns another site's cookies. |

The user's own clicks or typing in a run's tab can still disturb that run. ADR 0012 accepts that near-term. The Phase 7 end state, a bundled or custom browser that controls focus, hides run tabs, and blocks navigation during a run, is #631.

## What a remote-debugging port exposes

The first two bullets describe Chrome's known behavior. Neither has an official source, and neither was verified live for #595. A Chromium issue confirms that two instances can't share one user data directory.

- **One process per profile directory.** Chrome allows one browser process per `--user-data-dir`, guarded by the profile's singleton lock (the `SingletonLock`, `SingletonCookie` and `SingletonSocket` files that `scripts/chrome.sh` clears after a crash). Profiles inside that directory run in the same process. A second launch with the same directory hands its arguments to the running process and exits.
- **The port is browser-wide.** `/json/list`, `Target.getTargets` and `Target.setDiscoverTargets` report every page, worker, and extension target in the process. A client can attach to any of them and call `Runtime.evaluate`, read cookies (`Network.getAllCookies`, `Storage.getCookies`), take screenshots, and navigate.
- **There's no authentication.** Chrome binds the port to loopback and checks the `Host` header and WebSocket origins (`--remote-allow-origins`), but a native program on the same machine passes those checks.
- **What the UI adds.** A client on the port can read the UI's page, and after Gmail linkage, your Google cookies. That client can already drive LinkedIn and read LinkedIn's cookies, which is the asset netkeeper protects most. The API itself has no login today: `csrf_violation` (`netkeeper/web/security.py`) lets through a request with `X-Netkeeper-Client: 1` and no `Origin` or `Sec-Fetch-Site`, so a local program can already press any UI button without the port.
- **A per-install API secret (#618).** If #618 adds one, the UI's page holds it, so a client on the port could read it with `Runtime.evaluate`. That's acceptable for the same reason: a client on the port already controls LinkedIn. The secret still stops local programs that don't have the port.

## Profile and capture pollution (#520)

During the CP8c capture (the setup notes in #520), a netkeeper UI tab (`localhost:8000`) was open in the test-account capture profile, not in netkeeper's LinkedIn Chrome. It kept polling the API; the top bar alone refetches `/poll-status` every minute, and the UI holds an event stream open. The capture's netlog records the whole browser's traffic, so the UI's requests mixed in with LinkedIn's.

With the UI in netkeeper's Chrome:

- **The UI's contact links open LinkedIn here.** A click on a contact's `linkedin.com/in/...` link in the UI opens it in netkeeper's Chrome: the right identity, but your own browsing, outside netkeeper's budgets. Before, it opened in your everyday Chrome.
- **The UI never talks to LinkedIn.** Its requests go to localhost. LinkedIn's pages can't read the UI's origin, its storage, or the profile's history.
- **Runs don't see the UI's traffic,** because every network session is on the run's own tab.
- **The residual risk is a whole-browser capture,** such as a netlog or `chrome://net-export`, taken in netkeeper's Chrome. Close the UI and quit `serve` first, as #520's setup already says.
- **The profile now holds more:** the UI's origin data, and your Google sign-in after Gmail linkage. #633 keeps netkeeper's own reads to LinkedIn's cookies.

## The launcher (#636, for #549 and #599)

One function builds one argument list from config and spawns it:

- **Binary.** Run Chrome's binary directly, as an argument list with no shell. On macOS that's `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`; on Linux, `google-chrome`. Don't go through the printed `open -na "Google Chrome" --args ...`: `-n` does start a new process that receives the arguments, but the launcher needs that process's own exit status and no LaunchServices step in between.
- **Arguments.** `--remote-debugging-port=<port>` and `--user-data-dir=<profile>`, the same tokens `chrome_launch_command` prints, then `--app=<the UI's URL>`, then, from the first-run button only, LinkedIn's sign-in or feed URL as a positional argument. Never a headless flag, never a fresh profile, never another flag or URL. A test compares parsed arguments, not the printed shell lines with their trailing backslashes: the list equals the printed tokens plus these two additions, and checks that only the first-run caller passes the LinkedIn URL.
- **Already running.** When netkeeper's Chrome is up, the same command hands its arguments to the running process through the profile's singleton, which opens the UI's app window and exits. So `netkeeper open` works whether or not that Chrome is running. Not verified live: that the running process honors `--app` and the positional URL on a handoff.
- **Checks before a spawn.** Refuse, and report which case applied, as `scripts/chrome.sh --status` does, when:
  - the port answers but isn't the dedicated profile (#624's check), or
  - a live process holds the profile's singleton lock but the port doesn't answer (a Chrome on that profile without the port; a handoff would open the UI where runs can't attach).
- **Runs.** Starting Chrome cold is refused while a process holds the account's browser lock (checked through `services.runs`), and #599's button is disabled then. Opening the UI's window into a running Chrome is allowed during a run, because it's your act and touches no run tab.
- **Detached.** Run it detached, so the Chrome outlives `serve`.

### Why an `--app` window

An app window has no tab strip. A run's tab never opens among the UI's tabs, and the UI's window never switches to another tab. A run that raises its own window (#632) can still cover the UI's window on screen. Links the UI opens in a new tab, such as the Gmail wizard's Console links, open in a normal window of the same Chrome. Not verified live: whether Chrome ever places a run's `background: true` tab into an app window. #632 checks this.

### No "open in netkeeper's Chrome" hint

If you open the UI by hand in another browser, it works the same. It shows no hint. The page can't see its own window's debugging state, so a hint would need posture to list targets through the port and compare origins, and opening the UI elsewhere puts nothing at risk. #549's Chrome-required banner still applies to browsers that aren't Chrome. The old rule that a UI tab in netkeeper's Chrome is a posture warning is dropped: that tab is now expected.

## The safety scanner and the text that changes on acceptance

`tests/test_browser_safety.py` enforces ADR 0002's no-launch rule. It bans Playwright's launch calls, the `subprocess`, `pty` and `webbrowser` modules, and every process-spawning call (`create_subprocess_exec`, `Popen`, `posix_spawn`, and the rest) anywhere in the package. The launcher breaks that ban, so on acceptance it gets one exception:

- One module, and one function in it, may spawn a process. Nothing else in the package may, including the rest of that module.
- The function builds the launcher's one argument list from config. Tests pin it to the lines netkeeper prints, plus `--app=<the UI's URL>` and the first-run LinkedIn URL.
- Playwright's launch calls stay banned everywhere, with no exception.
- **Where it lives:** beside `worker.py` (for example `netkeeper/chrome_spawn.py`), outside `web/`, `linkedin/`, `crm/` and `services/`, and importing nothing from `linkedin/`. Only `netkeeper/cli.py` (`netkeeper open`, and `browser launch --start` if it's added) and `netkeeper/worker.py` (`serve`'s wiring) import it. A test pins that list of importers, as `BROWSER_CALLERS` does for the provider.
- **How a route reaches it:** the way the runs API reaches the worker. `serve` puts the spawner on `app.state` behind a Protocol in `services/`, as it does with `services.runs.RunExecutor`. The route submits the spawn to the task runner and returns at once. With no spawner on `app.state`, the route answers 503, as `_executor` does. A route never imports the module and never spawns inside a handler: the checks before a spawn can take seconds, and keeping process creation out of `web/` keeps the scanner's exception to one module.

This other text says netkeeper never starts Chrome, or that the UI belongs elsewhere, and changes to match when ADR 0012 is accepted:

- `docs/architecture.md`: F1, N2, and section 9.1, "Browser attach"
- `docs/guide/setup.md` (the "LinkedIn Chrome" bullet) and `docs/guide/troubleshooting.md` (the "Start the LinkedIn Chrome" row)
- `docs/implementation-guide.md`, wherever it says netkeeper never starts a browser
- `netkeeper/cli.py`: the `browser` group's help, `browser launch`'s docstring and message, and `_present_authorization_url`'s docstring
- `netkeeper/web/api/linkedin.py`: the `GET /linkedin/browser` docstring
- `scripts/chrome.sh` (its header comment)
- `CLAUDE.md`: the "Browser identity" line and the `make chrome` note
- ADR 0002: a dated amendment
