# Chrome profiles: research and mechanics

This document holds the evidence and the mechanics behind [ADR 0012](../adr/0012-chrome-profiles.md). The ADR holds the decision. Researched 2026-10-17 for #595.

## How netkeeper attaches today

- `linkedin.cdp_url` (default `http://127.0.0.1:9222`) names the port. `AttachProvider` in `netkeeper/linkedin/browser.py` connects with Playwright's `connect_over_cdp` and reuses `browser.contexts[0]`. It never creates a context, and it never closes the browser or the context.
- The profile is `<data dir>/chrome-profile` (`CHROME_PROFILE_DIRNAME`). `netkeeper browser launch` and `GET /linkedin/browser` print the command from `chrome_launch_command` in `netkeeper/services/browser_launch.py`: `--remote-debugging-port=<port>` and `--user-data-dir=<profile>`, and nothing else. `scripts/chrome.sh` (`make chrome`) runs that command for a person.

## What runs do with tabs

- **Open:** each run opens one tab with `Target.createTarget` and `background: true` (#195, PR #468). If that fails, it falls back to `context.new_page()`, which opens a foreground tab, and it logs an "opened in front" note.
- **Bring forward:** prefill and auto-send call `bring_to_front` once, before the Message click (ADR 0007, ADR 0008). This activates the Chrome window that holds the tab.
- **Close:** a run closes its own tab at the end, except a prefill, which hands its tab to the user. Auto-send closes the sent message bubble by its close control (#516), and the close rule names both participants.
- **Read other tabs:** `bubble_check` (`message-check --bubble`, #495) lists `context.pages` and reads only the tabs on LinkedIn's origin.

If the UI shares `contexts[0]`, each of these paths has to keep ignoring the UI's tabs. Today, the origin filter and the run-owned-tab rule do that, but every new tab path must keep doing it.

## What a remote-debugging port exposes

- **One process per profile directory.** Chrome allows one browser process per `--user-data-dir`, guarded by the profile's singleton lock (the `SingletonLock`, `SingletonCookie` and `SingletonSocket` files that `scripts/chrome.sh` clears after a crash). Profiles inside that directory (`--profile-directory=Profile 1`, and so on) run in the same process. A second launch with the same directory hands its arguments to the running process and exits.
- **The port is browser-wide.** `/json/list`, `Target.getTargets` and `Target.setDiscoverTargets` report every page, worker, and extension target in the process, across all of its profiles. A client can attach to any of them and call `Runtime.evaluate`, read cookies (`Network.getAllCookies`, `Storage.getCookies`), take screenshots, and navigate.
- **There's no authentication.** Any local process that reaches the port has the access above. Chrome checks the `Host` header on HTTP endpoints and rejects WebSocket upgrades from web origins that aren't allowed (`--remote-allow-origins`), but a native program on the same machine passes both checks.
- **Two profiles in one process are still one exposure.** A second profile under the same `--user-data-dir` doesn't remove anything from the port. Only a separate `--user-data-dir`, run as a separate process without the port flag, is outside the port.

## Chrome's recent restrictions

- **Chrome 136** (2025) ignores `--remote-debugging-port` and `--remote-debugging-pipe` when Chrome runs on the default user data directory. The flags work only with a non-default `--user-data-dir`, which uses its own encryption key. Chrome made this change because attackers used remote debugging to steal cookies after App-Bound Encryption shipped. Source: [Changes to remote debugging switches to improve security](https://developer.chrome.com/blog/remote-debugging-port).
- **Chrome 144** added an opt-in at `chrome://inspect/#remote-debugging` that lets a tool such as Chrome DevTools MCP connect to the user's running browser. Chrome asks the user to accept each session and shows a "controlled by automated test software" banner while one is active. Sources: [Let your coding agent debug your browser session](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session) and [Auto-connect](https://developer.chrome.com/docs/devtools/agents/use-cases/auto-connect).

For netkeeper, this means two things:

- The LinkedIn Chrome must keep its own `--user-data-dir`, which it already does.
- Your everyday Chrome has no port unless you turn on the opt-in, and then Chrome asks before each connection. That's the safe place for the UI.

## The CP8 incident

During the CP8c capture (the setup notes in #520), a netkeeper UI tab (`localhost:8000`) was open in the same Chrome profile as the capture. It kept polling the API; the top bar alone refetches `/poll-status` every minute, and the UI holds an event stream open. The capture recorded that traffic alongside LinkedIn's, so #520's setup tells you to quit `serve` and close every UI tab first.

## Opening the UI (for #549)

- **macOS:** run Chrome's binary directly, with `--app=<UI URL>` and no `--user-data-dir`. The binary is `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. With no `--user-data-dir`, the launch belongs to the default profile directory, so a running everyday Chrome receives it through that directory's singleton, and a stopped one starts. Don't use `open -a "Google Chrome" --args ...`: when two Chrome processes are running, LaunchServices can activate either one, and `--args` reaches only a process that the command itself launches.
- **Linux:** run `google-chrome --app=<UI URL>` without `--user-data-dir`.
- **No Chrome found:** open the URL in the default browser. #549's banner already says Chrome is required.
- **Never** pass the LinkedIn profile's `--user-data-dir` to the opener, and never ask the LinkedIn Chrome, over CDP or otherwise, to open a UI URL.

## Starting the LinkedIn Chrome (for #599)

- Run exactly `chrome_launch_command(port, profile)`, the same lines `browser launch` prints, as a detached process. #599's tests pin the two together.
- Don't start a second Chrome while the port answers, or while the profile's singleton lock is held by a live process. Report which case applies, as `scripts/chrome.sh --status` does.
- For the first run, add LinkedIn's sign-in URL as the start page. This is a URL argument, not a flag. Whether it counts as an added flag under ADR 0012's rule 3 is for the #599 review to confirm.

## Detecting a UI tab in the LinkedIn Chrome (rule 7)

Posture already attaches to read the session. While attached, it can compare `context.pages` URLs against the UI's own origin (scheme, host, and port), and report a warning row when one matches. It reads only the URLs and never touches the tab. The UI can show its own banner without the browser: the page can't see its own window's debugging state, so the banner comes from posture's row through the API.
