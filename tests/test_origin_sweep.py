"""#796: every place the LinkedIn package reads an href or a tab's url as a url.

``urlsplit`` and Chrome read some urls two ways: a backslash is a ``/`` to Chrome,
``///in/<slug>/`` has the host ``in`` to Chrome and none to ``urlsplit``, userinfo
and an encoded host hide the real one. Each site below that decides where a tab is,
what to click, or which link counts as LinkedIn's goes through
:func:`~netkeep.linkedin.strict_origin.on_origin` (or ``on_path``, or
``reads_one_way``) first. The guard at the end fails when a new ``urlsplit``,
``urlparse`` or ``urljoin`` call appears in these modules outside the reviewed list,
so the next one is reviewed too. Every slug, id and host here is invented.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import cast

import pytest
from flagship_pages import contact_info_payload
from voyager_pages import PEOPLE

from netkeep.linkedin import browser as browser_module
from netkeep.linkedin.browser import BrowserRun, read_card_link
from netkeep.linkedin.classify import Outcome
from netkeep.linkedin.flagship_profile import parse_contact_info
from netkeep.linkedin.inbox import InboxReadStopped
from netkeep.linkedin.observe import FAILURE_REDIRECT, ObservedResponse
from netkeep.linkedin.page_connections import PageConnections
from netkeep.linkedin.page_inbox import PageInbox
from netkeep.linkedin.page_profiles import PageProfiles
from netkeep.linkedin.strict_origin import LINKEDIN_ORIGIN, reads_one_way
from netkeep.linkedin.voyager import RouteChanged

SLUG = "fake-slug-qz"
ID = "ACoAAFakeIdQz0"
ROOT = Path(__file__).resolve().parents[1]

#: Each spelling Chrome reads as another origin than urlsplit does, with ``{path}``
#: where the path goes.
AMBIGUOUS = (
    "///{path}",  # Chrome reads the first segment, "in", as the host
    "https:///{path}",
    "https://evil.test\\@www.linkedin.com/{path}",
    "//evil.test\\@www.linkedin.com/{path}",
    "\\\\evil.test/{path}",
    "https://evil.test@www.linkedin.com/{path}",
    "https://evil.test%40www.linkedin.com/{path}",
    "https://evil.test\t@www.linkedin.com/{path}",
)


def _ambiguous(path: str) -> list[str]:
    return [spelling.format(path=path.lstrip("/")) for spelling in AMBIGUOUS]


def test_reads_one_way() -> None:
    for url in _ambiguous(f"/in/{SLUG}/"):
        assert reads_one_way(url) is False, url
    for url in (
        f"/in/{SLUG}/",
        f"in/{SLUG}/",
        f"https://www.linkedin.com/in/{SLUG}/?trk=a@b.test",
        f"//www.linkedin.com/in/{SLUG}/",
        "",
    ):
        assert reads_one_way(url) is True, url


# --- browser._is_profile_link: the bubble header's link ---------------------------------


@pytest.mark.parametrize("href", _ambiguous(f"/in/{ID}/"))
def test_a_bubble_header_link_read_two_ways_is_not_the_contact(href: str) -> None:
    assert browser_module._is_profile_link(href, f"/in/{ID}/", fold_case=False) is False


@pytest.mark.parametrize("href", [f"/in/{ID}/", f"/in/{ID}", f"https://www.linkedin.com/in/{ID}/"])
def test_an_ordinary_bubble_header_link_is_still_the_contact(href: str) -> None:
    assert browser_module._is_profile_link(href, f"/in/{ID}/", fold_case=False) is True


# --- browser.read_card_link: the never-messaged card's links -----------------------------


@pytest.mark.parametrize("href", _ambiguous(f"/in/{SLUG}/"))
def test_a_card_link_read_two_ways_is_someone_else(href: str) -> None:
    link = read_card_link(href, SLUG, ID)
    assert link is not None and not link.matches
    assert "unsafe path" in link.notes


@pytest.mark.parametrize(
    "href", [f"/in/{SLUG}/", f"https://www.linkedin.com/in/{SLUG.upper()}/", f"/in/{ID}/?x=1"]
)
def test_an_ordinary_card_link_still_matches(href: str) -> None:
    link = read_card_link(href, SLUG, ID)
    assert link is not None and link.matches


# --- page_profiles: where the tab landed, and a redirect's Location ----------------------


def _profiles() -> PageProfiles:
    return PageProfiles(cast(BrowserRun, object()))


@pytest.mark.parametrize("url", _ambiguous(f"/in/{SLUG}/"))
def test_a_profile_tab_read_two_ways_is_off_profile(url: str) -> None:
    landed = _profiles()._land_where(url)
    assert landed is not None and landed.outcome is Outcome.ROUTE_CHANGED


@pytest.mark.parametrize(
    "url", [f"{LINKEDIN_ORIGIN}/in/{SLUG}/", f"{LINKEDIN_ORIGIN}/in/{SLUG.upper()}?trk=x"]
)
def test_an_ordinary_profile_tab_still_lands(url: str) -> None:
    assert _profiles()._land_where(url) is None


def _redirect(location: str) -> ObservedResponse:
    return ObservedResponse(
        "GET",
        f"{LINKEDIN_ORIGIN}/in/{SLUG}/",
        301,
        "document",
        None,
        None,
        location=location,
        failure=FAILURE_REDIRECT,
    )


@pytest.mark.parametrize("location", _ambiguous("/in/fake-renamed-qz/"))
def test_a_redirect_location_read_two_ways_is_not_followed(location: str) -> None:
    answer = _profiles()._redirect(_redirect(location))
    assert answer is not None and answer.outcome is Outcome.ROUTE_CHANGED


@pytest.mark.parametrize(
    "location", ["/in/fake-renamed-qz/", f"{LINKEDIN_ORIGIN}/in/fake-renamed-qz/"]
)
def test_an_ordinary_redirect_to_a_renamed_profile_is_still_followed(location: str) -> None:
    assert _profiles()._redirect(_redirect(location)) is None


# --- page_connections and page_inbox: where the tab is -----------------------------------

CONNECTIONS = "/mynetwork/invite-connect/connections/"


@pytest.mark.parametrize("url", _ambiguous(CONNECTIONS))
def test_a_connections_tab_read_two_ways_stops(url: str) -> None:
    stopped = PageConnections(cast(BrowserRun, object()))._where(url)
    assert stopped is not None and stopped.outcome is Outcome.ROUTE_CHANGED


def test_an_ordinary_connections_tab_reads_on() -> None:
    reader = PageConnections(cast(BrowserRun, object()))
    assert reader._where(f"{LINKEDIN_ORIGIN}{CONNECTIONS}") is None
    assert reader._where(f"{LINKEDIN_ORIGIN}{CONNECTIONS.rstrip('/')}?x=1") is None


@pytest.mark.parametrize("url", [*_ambiguous("/messaging/"), *_ambiguous("/messaging/thread/x/")])
def test_an_inbox_tab_read_two_ways_stops(url: str) -> None:
    with pytest.raises(InboxReadStopped) as stopped:
        PageInbox(cast(BrowserRun, object()))._where(url, thread=True)
    assert stopped.value.outcome is Outcome.ROUTE_CHANGED


def test_an_ordinary_inbox_tab_reads_on() -> None:
    reader = PageInbox(cast(BrowserRun, object()))
    reader._where(f"{LINKEDIN_ORIGIN}/messaging/", thread=False)
    reader._where(f"{LINKEDIN_ORIGIN}/messaging/thread/2-fakeThreadQz/", thread=True)


# --- flagship_profile.parse_contact_info: whose overlay it is ----------------------------

PRIYA = PEOPLE[0]


def _overlay_linking(url: str) -> bytes:
    body = contact_info_payload(PRIYA, emails=["priya.fake@example.test"])
    linked = f"https://www.linkedin.com/in/{PRIYA.slug}".encode()
    assert body.count(linked) == 1
    escaped = url.replace("\\", "\\\\").replace("\t", "\\t")
    return body.replace(linked, escaped.encode())


@pytest.mark.parametrize("url", _ambiguous(f"/in/{PRIYA.slug}"))
def test_an_overlay_profile_link_read_two_ways_is_another_profile(url: str) -> None:
    with pytest.raises(RouteChanged, match="another profile"):
        parse_contact_info(_overlay_linking(url), slug=PRIYA.slug)


@pytest.mark.parametrize("url", [f"https://www.linkedin.com/in/{PRIYA.slug}", f"/in/{PRIYA.slug}/"])
def test_an_ordinary_overlay_profile_link_still_reads(url: str) -> None:
    info = parse_contact_info(_overlay_linking(url), slug=PRIYA.slug)
    assert info.emails == ("priya.fake@example.test",)


# --- the guard ---------------------------------------------------------------------------

#: Every ``urlsplit``/``urlparse``/``urljoin`` call in the guarded modules, by enclosing
#: function, with how many there are and why each is safe (#796). A new call fails
#: :func:`test_no_unreviewed_url_parsing`: route it through ``strict_origin``, or add it
#: here with its reason.
REVIEWED: dict[tuple[str, str, str], tuple[int, str]] = {
    # strict_origin itself: the strict reading the rest goes through.
    ("strict_origin.py", "parse_strict_origin", "urlsplit"): (1, "after raw refusals"),
    ("strict_origin.py", "_authority_origin", "urlsplit"): (1, "authority, then strict"),
    ("strict_origin.py", "reads_one_way", "urlsplit"): (1, "after raw refusals"),
    ("strict_origin.py", "on_origin", "urlsplit"): (1, "after reads_one_way"),
    ("strict_origin.py", "on_path", "urlsplit"): (1, "after on_origin"),
    ("strict_origin.py", "on_path", "urljoin"): (1, "after on_origin"),
    ("strict_origin.py", "url_path", "urlsplit"): (1, "path only, for a known url"),
    # browser.py
    ("browser.py", "_is_contact_compose", "urlsplit"): (1, "after on_origin"),
    ("browser.py", "_is_profile_link", "urlsplit"): (1, "after on_origin"),
    ("browser.py", "read_card_link", "urlsplit"): (1, "scheme first, then reads_one_way"),
    ("browser.py", "href_shape", "urlsplit"): (1, "diagnostic words, after the verdict"),
    ("browser.py", "CdpIdentityReader.read", "urlsplit"): (1, "CDP url, Python connects"),
    ("browser.py", "_is_browser_websocket", "urlsplit"): (1, "exact netloc, Python connects"),
    ("browser.py", "ui_window_target", "urlsplit"): (1, "Chrome-serialized, exact netloc"),
    # page_profiles.py
    ("page_profiles.py", "PageProfiles._slug_on_origin", "urlsplit"): (1, "after on_origin"),
    ("page_profiles.py", "PageProfiles._at_missing_page", "urlsplit"): (1, "after on_origin"),
    ("page_profiles.py", "PageProfiles._land_where", "urlsplit"): (1, "after on_origin"),
    ("page_profiles.py", "PageProfiles._landed_on_missing_page", "urlsplit"): (1, "own url"),
    ("page_profiles.py", "PageProfiles._redirect", "urljoin"): (1, "followed after on_origin"),
    ("page_profiles.py", "PageProfiles._take_other", "urljoin"): (1, "wall classify only"),
    ("page_profiles.py", "PageProfiles._read_overlay", "urljoin"): (1, "wall classify only"),
    ("page_profiles.py", "_answer_slug", "urlsplit"): (1, "observed request, origin-matched"),
    # the other readers
    ("page_connections.py", "PageConnections._where", "urlsplit"): (1, "after on_origin"),
    ("page_inbox.py", "PageInbox._where", "urlsplit"): (1, "after on_origin"),
    ("page_inbox.py", "PageInbox._take", "urlsplit"): (1, "observed request, origin-matched"),
    ("observe.py", "ResponseMatch.matches", "urlsplit"): (2, "Chrome-serialized request url"),
    ("messaging.py", "read_compose_option", "urlsplit"): (1, "observed request path"),
    ("messaging.py", "conversation_from_thread_request", "urlsplit"): (1, "observed request"),
    ("messaging.py", "is_create_message", "urlsplit"): (1, "observed request path"),
    ("messaging_shapes.py", "_query_params", "urlsplit"): (1, "observed request query"),
    ("messaging_shapes.py", "_thread_path", "urlsplit"): (1, "answer data, path only"),
    ("flagship_profile.py", "parse_contact_info", "urlsplit"): (1, "after on_origin"),
    ("flagship_profile.py", "_websites", "urlsplit"): (2, "a contact's site, stored as data"),
    ("flagship_profile.py", "_soft_handles", "urlsplit"): (1, "a handle, stored as data"),
    ("rehearse.py", "RequestRecord.host", "urlsplit"): (1, "rehearsal report only"),
    ("rehearse.py", "RequestRecord.path", "urlsplit"): (1, "rehearsal report only"),
    ("rehearse.py", "_Recorder._on_request", "urlsplit"): (1, "groups the report only"),
    ("rehearse.py", "_ReplicaHandler.do_GET", "urlsplit"): (1, "local replica server"),
    ("rehearse.py", "_ReplicaHandler.do_POST", "urlsplit"): (1, "local replica server"),
}

_PARSERS = frozenset({"urlsplit", "urlparse", "urljoin"})


def _guarded() -> list[Path]:
    return [*sorted((ROOT / "netkeep" / "linkedin").glob("*.py")), ROOT / "netkeep" / "worker.py"]


def _references(tree: ast.AST) -> Counter[tuple[str, str]]:
    """Every read of a parser's name, by enclosing function: a call, and also a name
    passed on (``map(urlsplit, ...)``) or bound to another (``split = urlsplit``),
    bare or as an attribute (``urllib.parse.urlsplit``). The string of one
    (``getattr(parse, "urlsplit")``) counts under ``"<string>"``, which nothing
    reviews."""
    found: Counter[tuple[str, str]] = Counter()

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = f"{scope}.{child.name}" if scope else child.name
            where = scope or "<module>"
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id in _PARSERS:
                    found[(where, child.id)] += 1
            elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                if child.attr in _PARSERS:
                    found[(where, child.attr)] += 1
            elif isinstance(child, ast.Constant) and child.value in _PARSERS:
                found[(where, "<string>")] += 1
            walk(child, inner)

    walk(tree, "")
    return found


def test_no_unreviewed_url_parsing() -> None:
    seen: dict[tuple[str, str, str], int] = {}
    for path in _guarded():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.parse":
                renamed = [a.name for a in node.names if a.asname and a.name in _PARSERS]
                assert not renamed, f"{path.name} imports {renamed} under another name"
        for (scope, name), count in _references(tree).items():
            seen[(path.name, scope, name)] = count
    unreviewed = {key: n for key, n in seen.items() if REVIEWED.get(key, (0, ""))[0] != n}
    assert not unreviewed, (
        "a url is parsed outside strict_origin where nobody reviewed it (#796): route it"
        f" through on_origin/on_path, or add it to REVIEWED with its reason: {unreviewed}"
    )
    gone = sorted(set(REVIEWED) - set(seen))
    assert not gone, f"REVIEWED lists calls that are gone; remove them: {gone}"


def test_the_guard_sees_a_new_call() -> None:
    source = "from urllib.parse import urlsplit\ndef f(u):\n    return urlsplit(u).hostname\n"
    assert _references(ast.parse(source)) == Counter({("f", "urlsplit"): 1})
    method = "class C:\n    def m(self, u):\n        return urllib.parse.urljoin(u, '/')\n"
    assert _references(ast.parse(method)) == Counter({("C.m", "urljoin"): 1})


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f(hrefs):\n    return list(map(urlsplit, hrefs))\n", ("f", "urlsplit")),
        ("def f(u):\n    split = urlsplit\n    return split(u)\n", ("f", "urlsplit")),
        ("def f(u):\n    return getattr(parse, 'urlparse')(u)\n", ("f", "<string>")),
        ("def f(u):\n    return getattr(urllib.parse, 'urljoin')(u, '/')\n", ("f", "<string>")),
        ("SPLIT = urlsplit\n", ("<module>", "urlsplit")),
    ],
    ids=["map", "rebound", "getattr", "getattr on urllib.parse", "module alias"],
)
def test_the_guard_sees_a_parser_passed_on_rebound_or_named(
    source: str, expected: tuple[str, str]
) -> None:
    """#799 review: a parser used without a direct call is still a reference."""
    assert _references(ast.parse(source))[expected] == 1


def test_a_parser_named_by_string_is_never_reviewed() -> None:
    assert not any(name == "<string>" for _, _, name in REVIEWED)
