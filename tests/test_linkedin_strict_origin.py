"""``netkeep.linkedin.strict_origin``: closing the urlsplit-vs-browser gap (F3).

A reviewer found that ``rehearse._require_neutral`` and ``fetch._require_fetchable_origin``
both trusted ``urlsplit`` alone, and both could be fooled the same way: a backslash
before an ``@`` reads as loopback to Python and as LinkedIn to Chrome. Every test here
is a case that must be refused before ``urlsplit`` sees it, or must resolve to the one
host a real browser would actually reach.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

from netkeep.linkedin import enrich, flagship
from netkeep.linkedin.strict_origin import (
    LINKEDIN_ORIGIN,
    NotAStrictOrigin,
    Origin,
    on_origin,
    on_path,
    parse_strict_origin,
    reads_one_way,
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


SLUG = "fake-slug-qz"
CONTACT_INFO = f"/in/{SLUG}/overlay/contact-info"
PROFILE_URL = f"{LINKEDIN_ORIGIN}/in/{SLUG}/"


@pytest.mark.parametrize(
    ("url", "path", "base", "at"),
    [
        (f"https://www.linkedin.com/in/{SLUG}/", f"/in/{SLUG}", LINKEDIN_ORIGIN, True),
        ("https://www.linkedin.com/in/Fake%2DSlug-QZ", f"/in/{SLUG}/", LINKEDIN_ORIGIN, True),
        (f"/in/{SLUG}", f"/in/{SLUG}", LINKEDIN_ORIGIN, True),
        (f"/in/{SLUG}", "/in/another-qz", LINKEDIN_ORIGIN, False),
        (f"https://evil.example.test/in/{SLUG}", f"/in/{SLUG}", LINKEDIN_ORIGIN, False),
        (f"http://www.linkedin.com/in/{SLUG}", f"/in/{SLUG}", LINKEDIN_ORIGIN, False),
        ("https://www.linkedin.com:444/in/x", "/in/x", LINKEDIN_ORIGIN, False),
        ("https://[::1/in/x", "/in/x", LINKEDIN_ORIGIN, False),
    ],
)
def test_on_path_compares_the_path_on_the_same_origin(
    url: str, path: str, base: str, at: bool
) -> None:
    """#719 review: the one path check the browser and the page readers share."""
    assert on_path(url, path, base=base) is at


@pytest.mark.parametrize(
    ("url", "base"),
    [
        # Relative, absolute, a trailing slash or none, a query, a fragment.
        (f"/in/{SLUG}/overlay/contact-info/", PROFILE_URL),
        (f"/in/{SLUG}/overlay/contact-info", PROFILE_URL),
        ("overlay/contact-info/", PROFILE_URL),
        (f"https://www.linkedin.com/in/{SLUG}/overlay/contact-info/", PROFILE_URL),
        (f"//www.linkedin.com/in/{SLUG}/overlay/contact-info/", PROFILE_URL),
        (f"/in/{SLUG}/overlay/contact-info/?trk=a@b.test", PROFILE_URL),
        (f"/in/{SLUG}/overlay/contact-info/#top", PROFILE_URL),
        # Case: the scheme, the host and the slug.
        (f"HTTPS://WWW.LinkedIn.com/in/{SLUG.upper()}/overlay/contact-info/", PROFILE_URL),
        # A percent-encoded slug, and a base that is the bare origin.
        ("/in/fake%2Dslug%2dqz/overlay/contact-info/", LINKEDIN_ORIGIN),
        (f"/in/{SLUG}/overlay/contact-info/", LINKEDIN_ORIGIN),
        (f"/in/{SLUG}/overlay/contact-info/", f"{LINKEDIN_ORIGIN}/"),
    ],
)
def test_on_path_still_takes_an_ordinary_linkedin_url(url: str, base: str) -> None:
    """#796: the refusals below leave every ordinary spelling of the same link alone."""
    assert on_path(url, CONTACT_INFO, base=base) is True


def test_on_path_reads_a_non_ascii_slug_either_way() -> None:
    profile = "/in/pr\u00edya-fake/"
    assert on_path(f"{LINKEDIN_ORIGIN}/in/pr%C3%ADya-fake/", profile, base=LINKEDIN_ORIGIN)
    assert on_path(f"{LINKEDIN_ORIGIN}{profile}", profile, base=LINKEDIN_ORIGIN)


@pytest.mark.parametrize(
    "url",
    [
        # The #795 review's example: urlsplit reads the host as LinkedIn, Chrome as evil.test.
        f"https://evil.test\\@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"//evil.test\\@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        # A backslash anywhere: Chrome reads it as '/'.
        f"https:\\\\evil.test/in/{SLUG}/overlay/contact-info/",
        f"\\\\evil.test/in/{SLUG}/overlay/contact-info/",
        f"/\\evil.test/in/{SLUG}/overlay/contact-info/",
        f"/in/{SLUG}\\overlay/contact-info/",
        # Userinfo, plain or with a password.
        f"https://evil.test@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"https://evil.test:pw@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"//evil.test@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"https://@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        # Encoded forms in the authority: an '@', a backslash, a slash, a host letter.
        f"https://evil.test%40www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"https://www.linkedin.com%40evil.test/in/{SLUG}/overlay/contact-info/",
        f"https://evil.test%5C@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"https://evil.test%5c%40www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"https://www.linkedin.com%2F@evil.test/in/{SLUG}/overlay/contact-info/",
        f"https://www.linked%69n.com/in/{SLUG}/overlay/contact-info/",
        # An empty authority: Chrome skips the slashes and takes "in" as the host.
        f"https:///in/{SLUG}/overlay/contact-info/",
        f"///in/{SLUG}/overlay/contact-info/",
        f"https:/in/{SLUG}/overlay/contact-info/",
        # A host that isn't ASCII.
        f"https://www.linkedin.c\u043em/in/{SLUG}/overlay/contact-info/",
        # A plain protocol-relative url on another host.
        f"//evil.example.test/in/{SLUG}/overlay/contact-info/",
        # Control characters and whitespace, which Chrome strips or drops.
        f"https://evil.test\t@www.linkedin.com/in/{SLUG}/overlay/contact-info/",
        f"https://evil.test\n/in/{SLUG}/overlay/contact-info/",
        f" /in/{SLUG}/overlay/contact-info/",
        f"/in/{SLUG}/overlay/contact-info/\x00",
        f"/in/{SLUG}/over lay/contact-info/",
        f"/in/{SLUG}/overlay/contact-info/\x7f",
    ],
)
def test_on_path_refuses_what_a_browser_could_read_another_way(url: str) -> None:
    """#796: refused as written, before the paths are compared, against either base."""
    assert on_path(url, CONTACT_INFO, base=PROFILE_URL) is False
    assert on_path(url, CONTACT_INFO, base=LINKEDIN_ORIGIN) is False


@pytest.mark.parametrize(
    "authority",
    [
        "https://www.linkedin.c\u043em",  # not ASCII
        "https://www.linked%69n.com",  # percent-encoded
        "https://u@www.linkedin.com",  # userinfo
    ],
)
def test_an_authority_is_refused_even_against_itself(authority: str) -> None:
    """#799 review: each rule refuses on its own, not only because the host differs
    from LinkedIn's. The url and the base name the same authority here."""
    url = f"{authority}/in/{SLUG}/"
    assert reads_one_way(url) is False
    assert reads_one_way(f"//{authority.split('://', 1)[1]}/in/{SLUG}/") is False
    assert on_origin(url, base=authority) is False
    assert on_origin(f"/in/{SLUG}/", base=authority) is False


@pytest.mark.parametrize("url", [f"https:///in/{SLUG}/", f"///in/{SLUG}/", f"https:/in/{SLUG}/"])
def test_an_empty_authority_does_not_read_one_way(url: str) -> None:
    """``https:///in/x``: Chrome skips the slashes and reads ``in`` as the host."""
    assert reads_one_way(url) is False
    assert on_origin(url, base=LINKEDIN_ORIGIN) is False


def test_a_protocol_relative_url_must_name_the_base_origin() -> None:
    """#799 review: ``//host/...`` is compared, not taken as relative."""
    url = f"//evil.example.test/in/{SLUG}/"
    assert reads_one_way(url) is True
    assert on_origin(url, base=LINKEDIN_ORIGIN) is False
    assert on_path(url, f"/in/{SLUG}/", base=PROFILE_URL) is False
    assert on_origin(f"//www.linkedin.com/in/{SLUG}/", base=LINKEDIN_ORIGIN) is True


def test_the_review_example_fooled_urlsplit() -> None:
    """The #795 review's href, which ``urlsplit`` alone takes as this profile's link."""
    href = f"https://evil.test\\@www.linkedin.com/in/{SLUG}/overlay/contact-info/"
    split = urlsplit(urljoin(PROFILE_URL, href))
    assert split.hostname == "www.linkedin.com"
    assert split.path == f"{CONTACT_INFO}/"
    assert on_path(href, CONTACT_INFO, base=PROFILE_URL) is False


@pytest.mark.parametrize(
    "base",
    [
        f"https://evil.test\\@www.linkedin.com/in/{SLUG}/",
        f"https://evil.test@www.linkedin.com/in/{SLUG}/",
        f"https://www.linkedin.com%2F/in/{SLUG}/",
        f"/in/{SLUG}/",
        "",
        "about:blank",
        f"https://www.linkedin.com/in/{SLUG}/\t",
    ],
)
def test_on_path_refuses_a_base_that_is_not_a_strict_origin(base: str) -> None:
    """The base is held to the same rules: it must name its origin unambiguously."""
    assert on_path(f"/in/{SLUG}/overlay/contact-info/", CONTACT_INFO, base=base) is False


def test_on_path_requires_the_origin() -> None:
    """#796: no caller can skip the origin. A foreign host at the right path is refused."""
    foreign = f"https://evil.test/in/{SLUG}/"
    assert on_path(foreign, f"/in/{SLUG}/", base=LINKEDIN_ORIGIN) is False
    assert on_path(foreign, f"/in/{SLUG}/", base="https://evil.test") is True
    with pytest.raises(TypeError):
        on_path(foreign, f"/in/{SLUG}/")  # type: ignore[call-arg]
