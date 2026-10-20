"""``netkeep/loopback.py``: the one loopback set and the cdp_url parse (#701)."""

from __future__ import annotations

import pytest

from netkeep.campaigns import gmail_oauth
from netkeep.loopback import LOOPBACK_HOSTS, cdp_url_problem, configured_port, host_of, is_loopback
from netkeep.services import post_install, posture, ui_settings
from netkeep.web import security


def test_the_loopback_set_is_pinned() -> None:
    """Written out, not read back from the module: widening it changes what the server
    answers and which Chrome netkeep attaches to."""
    assert frozenset({"127.0.0.1", "localhost", "::1"}) == LOOPBACK_HOSTS


def test_every_cdp_host_check_uses_the_one_set() -> None:
    """The copies are gone: each module holds this very object, not a lookalike."""
    assert security.LOOPBACK_HOSTNAMES is LOOPBACK_HOSTS
    for module in (posture, ui_settings, post_install, gmail_oauth):
        assert vars(module)["LOOPBACK_HOSTS"] is LOOPBACK_HOSTS, module.__name__


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://127.0.0.1:9222", "127.0.0.1"),
        ("http://LOCALHOST:9222", "localhost"),
        ("http://[::1]:9222", "::1"),
        ("http://10.0.0.5:9222", "10.0.0.5"),
        ("http://[::1", None),
        ("http://:9222", None),
        ("not a url", None),
    ],
)
def test_host_of(url: str, host: str | None) -> None:
    assert host_of(url) == host


@pytest.mark.parametrize(
    ("url", "loopback"),
    [
        ("http://127.0.0.1:9222", True),
        ("http://[::1]:9222", True),
        ("http://10.0.0.5:9222", False),
        ("http://127.0.0.1.example:9222", False),
        # Unparseable or hostless is not this machine: fail closed.
        ("http://[::1", False),
        ("http://:9222", False),
        ("", False),
    ],
)
def test_is_loopback_fails_closed(url: str, loopback: bool) -> None:
    assert is_loopback(url) is loopback


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("http://127.0.0.1:9333", 9333),
        ("http://127.0.0.1:65535", 65535),
        ("http://127.0.0.1", None),
        ("http://127.0.0.1:0", None),
        ("http://127.0.0.1:70000", None),
        ("http://127.0.0.1:port", None),
        ("http://[::1", None),
    ],
)
def test_configured_port_never_guesses(url: str, port: int | None) -> None:
    assert configured_port(url) == port


def test_a_well_formed_cdp_url_has_no_problem() -> None:
    assert cdp_url_problem("http://127.0.0.1:9222") is None
    assert cdp_url_problem("wss://10.0.0.5:9222") is None
