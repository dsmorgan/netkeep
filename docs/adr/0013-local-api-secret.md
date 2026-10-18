# 0013. A per-install key for the local API, handed to the browser as a cookie

Date: 2026-10-17

## Status

Proposed (#618).

## Context

The API binds to loopback with no login. Existing checks stop DNS rebinding and cross-site requests (`web/security.py`). Nothing stops a local program (a script, a malicious package, an SSRF in another local server) from calling every endpoint, including arming auto-send. The maintainer chose a per-install key on one condition: it's seamless, and nothing that works today breaks.

A key can't stop a process running as you. It can read the database file, read the Keychain (prompt-free through netkeeper's own Python), and drive netkeeper's Chrome, UI included, through its debugging port. So the gain is minor. The key stops programs that only speak HTTP to a local port, not a program written to attack netkeeper.

## Options

- **a. Put the key in `index.html`.** Any local program can read it there, so it adds nothing.
- **b. A one-time link exchanged for a cookie.** Whatever opens the UI (`netkeeper open`, or ADR 0012's launcher) puts a short-lived, single-use code in the URL. The server swaps the code for an `HttpOnly`, `SameSite=Strict` cookie. The browser sends it on every request, including the event stream and downloads, which can't carry custom headers.
- **c. Unix-socket peer credentials.** Browsers can't use a socket.

## Decision

Option b, plus a first-browser claim, so an existing bookmark keeps working.

1. **One key per data directory, kept only in the Keychain,** under #587's service. `serve` creates it on its first start. *Reason:* two data directories never share a key.
2. **Every `/api/` request needs the session cookie or `Authorization: Bearer <key>`, or it gets `401`.** The Host and CSRF checks still run. *Reason:* the cookie is for browsers, the header for local clients; neither works across sites.
3. **A few paths stay open:** health, session status, the connect link, the claim, the Gmail OAuth callback, and the static UI files. *Reason:* health is the launchd agent's probe. Google's redirect to the callback is cross-site, so a `Strict` cookie never arrives, and the callback has its own one-time `state`. The UI files hold no data.
4. **The cookie holds a value derived from the key, never the key.** It survives restarts, and each UI load renews it. *Reason:* you connect a browser once, and the browser never holds the key.
5. **A connect code expires within minutes, works once, and stays out of the access log.** *Reason:* it passes through Chrome's command line and history.
6. **The first browser to load the UI after a key is created connects without a link.** Every browser after that needs `netkeeper open`. *Reason:* an upgrade or a fresh install has no link to follow. If another program claims first, you see the connect page and notice.
7. **A browser that isn't connected gets a clear page, not a dead end.** It shows the exact `netkeeper open` command with a copy button. *Reason:* a bare URL that connected itself would let every local program in too.
8. **When the Keychain refuses, `serve` still starts, on a key in memory, and says so.** The UI shows a banner, and the connect page and `netkeeper open` explain that restarting reconnects the first browser. *Reason:* failing closed would break seamlessness; failing silently would leave a dead end.

The mechanics are in [architecture.md, section 14.2](../architecture.md#142-local-security).

## Consequences

- **What you notice:** nothing. Your browser connects itself on its first load; a second browser shows the connect page once.
- **ADR 0012's launcher (#636, #549, #599):** its `--app=` URL is `local_api_key.connect_url()`, so every window it opens is connected.
- **Dev mode:** the Vite proxy forwards `/api`, so `pnpm dev` works the same way.
- **Unverified:** `serve` now reads the Keychain on every start. If the Python that made the item changes, macOS may prompt then.
- **Keep true:** no response carries the key, and no page script can read the cookie. A path joins the open list only when it reads no data and changes nothing, or when it carries its own one-time proof.
- **Follow-up:** rotating the key, which disconnects every browser, isn't built yet.
