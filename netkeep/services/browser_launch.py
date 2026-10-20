"""The Chrome launch command, as data, for ``GET /linkedin/browser`` (P2-12).

``netkeep browser launch`` (``netkeep/cli.py``) prints this same command from a
terminal -- literally: it calls :func:`chrome_launch_command` and
:func:`netkeep.loopback.configured_port` too, rather than keeping its own copy, so
there is exactly one place that knows how to build the command and the two can never
drift apart. This module exists separately so the LinkedIn page can show the same
thing, without the route that serves it importing anything that touches a browser:
``netkeep.linkedin.browser`` is one of ``BROWSER_MODULES`` in
``tests/test_browser_safety.py``, so nothing under ``netkeep/web/`` may import it,
even for a constant, and ``netkeep/cli.py`` is one of the few modules the safety
rules do allow to. Everything here is a pure string computation from config -- no
CDP, no Playwright, nothing awaited -- so a request handler may call it directly
(CLAUDE.md: "never `await` browser work inside a request handler").

``CHROME_PROFILE_DIRNAME`` is the one thing still duplicated from
``netkeep.linkedin.browser`` rather than imported, for the same reason an import
of the whole module is forbidden; ``tests/test_browser_launch.py`` pins the two
constants together so they cannot silently drift apart, and separately asserts that
``netkeep browser launch``'s own printed lines are exactly this module's output,
so an import forgetting to happen would fail loudly rather than pass by coincidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

from netkeep.loopback import LOOPBACK_HOSTS, host_of

#: Mirrors ``netkeep.linkedin.browser.CHROME_PROFILE_DIRNAME``. See the module
#: docstring for why this is a copy and not an import.
CHROME_PROFILE_DIRNAME = "chrome-profile"

#: Chrome's usual remote-debugging port: ``scripts/chrome.sh``'s fallback when it can't
#: ask netkeep (``tests/test_helper_scripts.py`` pins the two). netkeep itself never
#: uses it in place of a port ``cdp_url`` doesn't name (#701).
DEFAULT_CDP_PORT = 9222


def chrome_flags(port: int, profile: Path) -> list[str]:
    """The flags :func:`chrome_launch_command` prints, as arguments: the launcher's (#636).

    ``netkeep.chrome_spawn`` passes exactly these to Chrome's binary, then ``--app``;
    ``tests/test_chrome_spawn.py`` checks them against the printed lines, parsed.
    """
    return [f"--remote-debugging-port={port}", f"--user-data-dir={profile}"]


def chrome_launch_command(port: int, profile: Path) -> list[str]:
    """The platform's Chrome command, as lines a person can paste (spec 9.1).

    ``sys.platform`` is read on whichever machine runs this -- the backend and
    the Chrome it attaches to are the same machine today (spec 5, "Multi-user
    readiness"), so that is also the machine the person would paste this into.
    """
    platform: str = sys.platform
    opener = 'open -na "Google Chrome" --args \\' if platform == "darwin" else "google-chrome \\"
    return [opener, f"  --remote-debugging-port={port} \\", f'  --user-data-dir="{profile}"']


def no_port_note(cdp_url: str) -> str:
    """Why there is no command to show: ``cdp_url`` names no valid port (#701).

    Never Chrome's usual 9222 instead: a command on a port netkeep won't attach to
    starts a Chrome nothing uses.
    """
    return (
        f"linkedin.cdp_url ({cdp_url!r}) names no valid port, so there is no command to"
        " show. Set it to an address such as http://127.0.0.1:9222 in config.toml or on"
        " the Settings page"
    )


def remote_host_note(cdp_url: str) -> str | None:
    """Why the command above will not work here, when ``cdp_url`` points elsewhere.

    Chrome's debug port only answers on its own loopback address, so a
    ``cdp_url`` naming another host can never be reached this way -- the same
    check ``netkeep browser launch`` prints as a warning. ``None`` too for a url with
    no host or one that doesn't parse (#701): it names no port either, and every
    caller refuses that on its own, with the reason.
    """
    host = host_of(cdp_url)
    if host is None or host in LOOPBACK_HOSTS:
        return None
    return (
        f"linkedin.cdp_url points at {host}, not this machine. Chrome's debug port is only"
        " reachable on its own loopback address."
    )
