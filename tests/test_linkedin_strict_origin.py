"""``netkeep.linkedin.strict_origin``: closing the urlsplit-vs-browser gap (F3).

A reviewer found that ``rehearse._require_neutral`` and ``fetch._require_fetchable_origin``
both trusted ``urlsplit`` alone, and both could be fooled the same way: a backslash
before an ``@`` reads as loopback to Python and as LinkedIn to Chrome. Every test here
is a case that must be refused before ``urlsplit`` sees it, or must resolve to the one
host a real browser would actually reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netkeep.linkedin import enrich, flagship
from netkeep.linkedin.strict_origin import (
    LINKEDIN_ORIGIN,
    NotAStrictOrigin,
    Origin,
    on_path,
    parse_strict_origin,
    require_page_origin,
    trim_path,
    url_path,
)

# --- the attack this module exists to close -----------------------------------


@pytest.mark.parametrize(
    "value",
    [
        r"http://www.linkedin.com\@127.0.0.1:8080",
        r"http://evil.example\@127.0.0.1",
        r"https://www.linkedin.com\@127.0.0.1",
        r"http://127.0.0.1\@www.linkedin.com",
    ],
)
def test_a_backslash_before_userinfo_is_refused_outright(value: str) -> None:
    """The exact WHATWG-vs-urlsplit differential the reviewer demonstrated.

    Python's urlsplit would read every one of these as pointing at whichever host
    follows the last '@' -- the opposite of where a real browser, which folds '\\'
    into '/' for http(s), would actually go. Refusing on sight is the only fix that
    does not require trusting urlsplit's answer for the case that matters most.
    """
    with pytest.raises(NotAStrictOrigin, match="backslash"):
        parse_strict_origin(value)


def test_urlsplit_would_have_been_fooled_by_the_backslash_case() -> None:
    """Proof the vulnerability was real, not hypothetical -- pinned so it cannot regress."""
    from urllib.parse import urlsplit

    fooled = urlsplit(r"http://www.linkedin.com\@127.0.0.1:8080")
    assert fooled.hostname == "127.0.0.1", (
        "if this ever stops being true, the module docstring's claim needs updating"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://user@127.0.0.1",
        "http://user:pass@127.0.0.1",
        "https://www.linkedin.com@evil.example",
        "http://@127.0.0.1",
    ],
)
def test_userinfo_without_a_backslash_is_also_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin, match="userinfo"):
        parse_strict_origin(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1/path",
        "http://127.0.0.1/../evil",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#frag",
        "http://127.0.0.1/path?x=1#frag",
    ],
)
def test_anything_past_the_authority_is_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin, match=r"path|query|fragment"):
        parse_strict_origin(value)


def test_a_bare_trailing_slash_is_allowed() -> None:
    origin = parse_strict_origin("http://127.0.0.1:8080/")
    assert origin == Origin(scheme="http", host="127.0.0.1", port=8080)


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1 ",
        " http://127.0.0.1",
        "http://127.0.0.1\t",
        "http://127.0.0.1\n",
        "http://127.0.0.1\r",
        "http://127.0.0.1\x00",
        "http://127.0.0.1\x7f",
        "ht tp://127.0.0.1",
    ],
)
def test_whitespace_and_control_characters_are_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin):
        parse_strict_origin(value)


@pytest.mark.parametrize("value", ["", "not-a-url", "://127.0.0.1", "http://"])
def test_unparseable_or_schemeless_or_hostless_strings_are_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin):
        parse_strict_origin(value)


# --- what a valid origin rebuilds to --------------------------------------------


def test_a_plain_origin_round_trips() -> None:
    origin = parse_strict_origin("https://www.linkedin.com")
    assert origin == Origin(scheme="https", host="www.linkedin.com", port=None)
    assert str(origin) == "https://www.linkedin.com"


def test_a_port_is_kept_and_rebuilt() -> None:
    origin = parse_strict_origin("http://127.0.0.1:52341")
    assert origin.port == 52341
    assert str(origin) == "http://127.0.0.1:52341"


def test_an_ipv6_loopback_round_trips_with_its_brackets() -> None:
    origin = parse_strict_origin("http://[::1]:9999")
    assert origin.host == "::1"
    # The rebuilt form must still be a url a browser can parse: dropping the
    # brackets around an IPv6 literal would make ':' ambiguous with the port
    # separator, exactly the kind of self-inflicted parser confusion this module
    # exists to prevent.
    assert str(origin) == "http://[::1]:9999"


def test_hostname_is_lowercased_the_way_urlsplit_already_does() -> None:
    origin = parse_strict_origin("HTTP://WWW.LINKEDIN.COM")
    assert origin.host == "www.linkedin.com"


# --- the page readers' shared guard (#698) ----------------------------------------------


def test_the_page_guard_keeps_linkedin_and_loopback_only() -> None:
    reader = "the test reader"
    assert require_page_origin("https://www.linkedin.com/", reader=reader) is LINKEDIN_ORIGIN
    assert require_page_origin("http://127.0.0.1:4000", reader=reader) == "http://127.0.0.1:4000"
    assert require_page_origin("https://[::1]:9", reader=reader) == "https://[::1]:9"
    assert require_page_origin("http://localhost:8", reader=reader) == "http://localhost:8"
    for refused in (
        "https://linkedin.com",
        "http://www.linkedin.com",
        "https://www.linkedin.com.elsewhere.example.test",
        "ftp://127.0.0.1",
        "http://10.0.0.1",
    ):
        with pytest.raises(ValueError, match=f"{reader} opens .* never"):
            require_page_origin(refused, reader=reader)
    for malformed in ("https://www.linkedin.com/in/x", "http://www.linkedin.com\\@127.0.0.1"):
        with pytest.raises(ValueError, match=f"is not an origin {reader} may open"):
            require_page_origin(malformed, reader=reader)


def test_linkedin_origin_has_one_definition() -> None:
    """``enrich`` and ``flagship`` re-export the one constant rather than spelling it."""
    assert LINKEDIN_ORIGIN == "https://www.linkedin.com"
    assert enrich.LINKEDIN_ORIGIN is LINKEDIN_ORIGIN is flagship.LINKEDIN_ORIGIN
    for module in (enrich, flagship):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert '"https://www.linkedin.com"' not in source, module.__name__


@pytest.mark.parametrize(
    ("value", "trimmed", "path"),
    [
        ("/a/b/", "/a/b", "/a/b"),
        ("/", "/", "/"),
        ("", "", ""),
        ("https://h.test/x/?q=1", None, "/x"),
    ],
)
def test_the_path_trims_one_slash(value: str, trimmed: str | None, path: str) -> None:
    if trimmed is not None:
        assert trim_path(value) == trimmed
    assert url_path(value) == path


@pytest.mark.parametrize(
    ("url", "path", "base", "at"),
    [
        ("https://www.linkedin.com/in/fake-slug-qz/", "/in/fake-slug-qz", None, True),
        ("https://www.linkedin.com/in/Fake%2DSlug-QZ", "/in/fake-slug-qz/", None, True),
        ("/in/fake-slug-qz", "/in/fake-slug-qz", LINKEDIN_ORIGIN, True),
        ("/in/fake-slug-qz", "/in/another-qz", LINKEDIN_ORIGIN, False),
        ("https://evil.example.test/in/fake-slug-qz", "/in/fake-slug-qz", LINKEDIN_ORIGIN, False),
        ("http://www.linkedin.com/in/fake-slug-qz", "/in/fake-slug-qz", LINKEDIN_ORIGIN, False),
        ("https://www.linkedin.com:444/in/x", "/in/x", LINKEDIN_ORIGIN, False),
        ("https://[::1/in/x", "/in/x", None, False),
    ],
)
def test_on_path_compares_the_path_on_the_same_origin(
    url: str, path: str, base: str | None, at: bool
) -> None:
    """#719 review: the one path check the browser and the page readers share."""
    assert on_path(url, path, base=base) is at
