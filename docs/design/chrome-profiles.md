# Chrome profiles: research and mechanics

This document holds the evidence and the mechanics behind [ADR 0012](../adr/0012-chrome-profiles.md). The ADR holds the decision. Researched 2026-10-17 for #595.

## How netkeeper attaches today

- `linkedin.cdp_url` (default `http://127.0.0.1:9222`) names the port. `AttachProvider` in `netkeeper/linkedin/browser.py` connects with Playwright's `connect_over_cdp` and reuses `browser.contexts[0]`. It never creates a context, and it never closes the browser or the context.
- The profile is `<data dir>/chrome-profile` (`CHROME_PROFILE_DIRNAME`). `netkeeper browser launch` and `GET /linkedin/browser` print the command from `chrome_launch_command` in `netkeeper/services/browser_launch.py`: `--remote-debugging-port=<port>` and `--user-data-dir=<profile>`, and nothing else. `scripts/chrome.sh` (`make chrome`) runs that command for a person.

## What runs do with tabs

- **Open:** each run opens one tab with `Target.createTarget` and `background: true` (#195, PR #468). If that fails, it falls back to `context.new_page()`, which opens a foreground tab, and it logs an "opened in front" note.
- **Bring forward:** prefill and auto-send call `bring_to_front` once, before the Message click (ADR 0007, ADR 0008). This activates the Chrome window that holds the tab.
- **Close:** a run closes its own tab at the end, except a prefill, which hands its tab to the user. Auto-send closes the sent message bubble by its close control. Past the control's fixed prefix, the rule accepts the contact's name alone, or with one other name on either side of " and " (#516, `_close_by_rule`).
- **Read other tabs:** `bubble_check` (`message-check --bubble`, #495) lists `context.pages` and reads only the tabs on LinkedIn's origin.

If the UI shares `contexts[0]`, each of these paths has to keep ignoring the UI's tabs. Today, the origin filter and the run-owned-tab rule do that, but every new tab path must keep doing it.

## What a remote-debugging port exposes

The first two bullets describe Chrome's known behavior. Neither has an official source, and neither was verified live for #595. A Chromium issue confirms that two instances can't share one user data directory. If a second profile's tabs show up through the port, they show up as a second browser context, and `AttachProvider` already logs a warning when the attached Chrome has more than one context (`browser.py`, the `len(contexts) > 1` branch). That warning is the observable sign.

- **One process per profile directory.** Chrome allows one browser process per `--user-data-dir`, guarded by the profile's singleton lock (the `SingletonLock`, `SingletonCookie` and `SingletonSocket` files that `scripts/chrome.sh` clears after a crash). Profiles inside that directory (`--profile-directory=Profile 1`, and so on) run in the same process. A second launch with the same directory hands its arguments to the running process and exits.
- **The port is browser-wide.** `/json/list`, `Target.getTargets` and `Target.setDiscoverTargets` report every page, worker, and extension target in the process, across all of its profiles. A client can attach to any of them and call `Runtime.evaluate`, read cookies (`Network.getAllCookies`, `Storage.getCookies`), take screenshots, and navigate.
- **There's no authentication.** Any local process that reaches the port has the access above. Chrome checks the `Host` header on HTTP endpoints and rejects WebSocket upgrades from web origins that aren't allowed (`--remote-allow-origins`), but a native program on the same machine passes both checks.
- **For the UI, this adds nothing new.** The API has no login, and `csrf_violation` (`netkeeper/web/security.py`) lets through a request that carries `X-Netkeeper-Client: 1` and has no `Origin` or `Sec-Fetch-Site`, which is a plain `curl` to loopback. A local program can already press any UI button without the port. That's why ADR 0012 rests on tabs, focus, and pollution, not on the port.
- **Two profiles in one process are still one exposure.** A second profile under the same `--user-data-dir` doesn't remove anything from the port. Only a separate `--user-data-dir`, run as a separate process without the port flag, is outside the port.

## Chrome's recent restrictions

- **Chrome 136** (2025) ignores `--remote-debugging-port` and `--remote-debugging-pipe` when Chrome runs on the default user data directory. The flags work only with a non-default `--user-data-dir`, which uses its own encryption key. Chrome made this change because attackers used remote debugging to steal cookies after App-Bound Encryption shipped. Source: [Changes to remote debugging switches to improve security](https://developer.chrome.com/blog/remote-debugging-port).
- **Chrome 144** added an opt-in at `chrome://inspect/#remote-debugging` that lets a tool such as Chrome DevTools MCP connect to the user's running browser. Chrome asks the user to accept each session and shows a "controlled by automated test software" banner while one is active. Sources: [Let your coding agent debug your browser session](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session) and [Auto-connect](https://developer.chrome.com/docs/devtools/agents/use-cases/auto-connect).

For netkeeper, this means two things:

- The LinkedIn Chrome must keep its own `--user-data-dir`, which it already does.
- Your everyday Chrome has no port unless you turn on the opt-in, and then Chrome asks before each connection. That's the safe place for the UI.

## The CP8 incident

During the CP8c capture (the setup notes in #520), a netkeeper UI tab (`localhost:8000`) was open in the same Chrome profile as the capture. That was the test-account capture profile (#484 and #520, setup steps 1 and 2), not netkeeper's LinkedIn Chrome. It kept polling the API; the top bar alone refetches `/poll-status` every minute, and the UI holds an event stream open. The capture recorded that traffic alongside LinkedIn's, so #520's setup tells you to quit `serve` and close every UI tab first.

## Opening the UI (for #549)

- **macOS:** run Chrome's binary directly, as an argument list (no shell), with `--app=<UI URL>` and no `--user-data-dir`. The binary is `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. With no `--user-data-dir`, the launch belongs to the default profile directory, so a running everyday Chrome receives it through that directory's singleton, and a stopped one starts. Don't use `open -a "Google Chrome" --args ...`: when two Chrome processes are running, LaunchServices can activate either one, and `--args` reaches only a process that the command itself launches.
- **Linux:** run `google-chrome --app=<UI URL>` without `--user-data-dir`.
- **No Chrome found:** open the URL in the default browser. #549's banner already says Chrome is required.
- **Never** pass the LinkedIn profile's `--user-data-dir` to the opener, and never ask the LinkedIn Chrome, over CDP or otherwise, to open a UI URL.

## Starting the LinkedIn Chrome (for #599)

- Don't run the printed lines through a shell. `chrome_launch_command` returns shell lines with trailing backslashes, for a person to paste. Build an argument list from the same port and profile, and pin it in a test to the printed lines: each flag and value matches, and nothing is added.
- On macOS, the printed first line is `open -na "Google Chrome" --args`. That's the LaunchServices route the opener avoids, but `-n` always starts a new instance, so `--args` reaches it, and the profile's singleton lock refuses a second one on the same profile. Either that route or the binary directly is acceptable, as long as the test pins the argument list.
- Run it detached, so the Chrome outlives `serve`.
- Don't start a second Chrome while the port answers, or while the profile's singleton lock is held by a live process. Report which case applies, as `scripts/chrome.sh --status` does.
- ADR 0012's rule 4 allows one start URL: LinkedIn's own sign-in or feed URL, as a positional argument after the flags, never a flag, and only from the first-run button. Start-at-login and the ordinary **Start Chrome** button pass no URL. A test pins the allowed URLs and checks that no other caller passes one.

## Detecting a UI tab in the LinkedIn Chrome (rule 8)

Posture already attaches to read the session. While attached, it can compare `context.pages` URLs against the UI's own origin (scheme, host, and port), and report a warning row when one matches. It reads only the URLs and never touches the tab. The UI can show its own banner without the browser: the page can't see its own window's debugging state, so the banner comes from posture's row through the API.

## The safety scanner and the text that changes on acceptance

`tests/test_browser_safety.py` enforces ADR 0002's no-launch rule. It bans Playwright's launch calls, the `subprocess`, `pty` and `webbrowser` modules, and every process-spawning call (`create_subprocess_exec`, `Popen`, `posix_spawn`, and the rest) anywhere in the package. Both spawns in ADR 0012 break that ban, so on acceptance it gets one exception:

- One module, and one function in it, may spawn a process. Nothing else in the package may, including the rest of that module.
- The function builds one of two argument lists from config: the LinkedIn launcher's (rule 3, plus rule 4's start URL) and the UI opener's (rule 2). Tests pin each one to the lines netkeeper prints for it.
- Playwright's launch calls stay banned everywhere, with no exception.
- **Where it lives:** the spawning module sits beside `worker.py` (for example `netkeeper/chrome_spawn.py`), outside `web/`, `linkedin/`, `crm/` and `services/`, and imports nothing from `linkedin/`. Only `netkeeper/cli.py` (`netkeeper open`, and `browser launch --start` if it's added) and `netkeeper/worker.py` (`serve`'s wiring) import it. A test pins that list of importers, as `BROWSER_CALLERS` does for the provider, so no wrapper under `services/` can smuggle it in.
- **How a route reaches it:** the way the runs API reaches the worker. `serve` puts the spawner on `app.state` behind a Protocol in `services/`, as it does with `services.runs.RunExecutor`. The route submits the spawn to the task runner and returns at once. With no spawner on `app.state`, the route answers 503, as `_executor` does. A route never imports the module and never spawns inside a handler. The reason isn't spec 9.9's deadlock, because a spawn touches no tab. The reasons are that the checks before a spawn and the wait for the port can take seconds, and that keeping process creation out of `web/` keeps the scanner's exception to one module.
- **Checks before a spawn:** before it spawns the LinkedIn Chrome, the task refuses in three cases: a process holds the account's browser lock (checked through `services.runs`), the port already answers, or a live process holds the profile's singleton lock. It reports which case applied.

This other text says netkeeper never starts Chrome, and changes to match when ADR 0012 is accepted:

- `docs/architecture.md`: F1 (line 71), N2 (line 110), and section 9.1 (lines 438 to 450)
- `docs/guide/setup.md` (line 8) and `docs/guide/troubleshooting.md` (line 83)
- `docs/implementation-guide.md` (lines 145 and 407)
- `netkeeper/cli.py` (lines 197, 599 and 625)
- `netkeeper/web/api/linkedin.py` (line 908)
- `scripts/chrome.sh` (its header comment)
- `CLAUDE.md`: the "Browser identity" line and the `make chrome` note
