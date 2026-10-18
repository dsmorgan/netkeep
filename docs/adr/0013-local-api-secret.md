# 0013. A per-install key for the local API, handed to the browser as a cookie

Date: 2026-10-17

## Status

Proposed (#618).

## Context

The API binds to loopback with no login. A Host check stops DNS rebinding, and header and Origin checks stop cross-site requests (`web/security.py`). Nothing stops a program on the Mac: a script, a malicious package install, or a local server tricked into making requests (SSRF) can call every endpoint, including arming auto-send. The maintainer chose a per-install key on one condition: it's seamless. You never type, copy, or see a key, and nothing that works today breaks.

A key can't stop a process running as you. That process can read the database file, read the Keychain (with a prompt, or without one through netkeeper's own Python), and drive any Chrome with an open debugging port, including a UI tab in netkeeper's own Chrome. So the gain is minor. The key stops programs that only speak HTTP to a local port, not a program written to attack netkeeper.

## Options

- **a. Put the key in `index.html`.** Any local program can read it there, so it only stops cross-site pages, which are already stopped.
- **b. A one-time link exchanged for a cookie.** Whatever opens the UI (`netkeeper open`, or ADR 0012's launcher) puts a short-lived, single-use code in the URL. The server swaps the code for an `HttpOnly`, `SameSite=Strict` cookie. The browser sends it on every request, including the event stream and downloads, which can't carry custom headers.
- **c. Unix-socket peer credentials.** Browsers can't use a socket.

## Decision

Option b, plus a first-browser claim, so an existing bookmark keeps working.

1. **One key per data directory, kept only in the Keychain,** under #587's service. `serve` creates it on its first start. *Reason:* two data directories never share a key.
2. **Every `/api/` request needs the session cookie or `Authorization: Bearer <key>`, or it gets `401`.** The Host and CSRF checks still run. *Reason:* the cookie is for the browser and the header is for local clients. Neither one works across sites.
3. **A few paths stay open:** health, session status, the connect link, the claim, the Gmail OAuth callback, and the static UI files. *Reason:* health is the launchd agent's probe. Google's redirect to the callback is cross-site, so a `Strict` cookie never arrives, and the callback has its own one-time `state`. The UI files hold no data.
4. **The cookie holds a value derived from the key, never the key.** It lasts for months and survives restarts. *Reason:* you connect a browser once, and the browser never holds the key.
5. **A connect code expires within minutes, works once, and stays out of the access log.** *Reason:* it passes through Chrome's command line and history.
6. **The first browser to load the UI after a key is created connects without a link.** The UI does this on its own. Every browser after that needs `netkeeper open`. *Reason:* an upgrade or a fresh install has no link to follow. If another program claims first, you see the connect page and notice.
7. **A browser that isn't connected gets a clear page, not a dead end.** It shows `netkeeper open` with a copy button. *Reason:* a bare URL that connected itself would let every local program in too.

The mechanics are in [architecture.md, section 14.2](../architecture.md#142-local-security).

## Consequences

- **What you notice:** nothing. After the upgrade, your browser connects itself on its first load. A second browser or cleared cookies show the connect page once.
- **ADR 0012's launcher (#636, #549, #599):** its `--app=` URL is `local_api_key.connect_url()`, so every window it opens is connected. Then the connect page can offer a one-click **Open netkeeper**.
- **Dev mode:** the Vite proxy forwards `/api`, connect link included, so `pnpm dev` works the same way.
- **Keep true:** no response carries the key, and no page script can read the cookie. A path joins the open list only when it reads no data and changes nothing, or when it carries its own one-time proof.
- **Follow-up:** rotating the key, which disconnects every browser, isn't built yet.
