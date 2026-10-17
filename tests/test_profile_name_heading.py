"""#552: the profile's name from the top card's one ``h2`` when the page has no ``h1``.

The CP8d captures showed LinkedIn's profile with no ``h1``: the name is an ``h2`` with
hashed classes. :meth:`netkeeper.linkedin.browser.BrowserRun._name_heading` finds it from
the verified Contact info link, and the chip comparison stays exact. Invented pages
(:func:`messaging_pages.profile_top_card_html`), the prefill's own fakes.
"""

from __future__ import annotations

from typing import Any

import pytest
from messaging_dom import Bubble, MessagingSite
from messaging_pages import (
    THADDEUS,
    ZEPHYRINE,
    never_messaged_bubble_html,
    profile_top_card_html,
)
from test_prefill_page import assert_no_keys, prefill

from netkeeper.linkedin.messaging import MessageOutcomeKind

MBA = f"{ZEPHYRINE.name}, MBA"


def _h2_site(chip: str = ZEPHYRINE.name, **profile: Any) -> MessagingSite:
    """A profile with no ``h1`` (:func:`profile_top_card_html`), and the New message
    bubble whose chip names ``chip``."""
    html = never_messaged_bubble_html([ZEPHYRINE]).replace(
        f"Remove {ZEPHYRINE.name}", f"Remove {chip}"
    )
    return MessagingSite(
        ZEPHYRINE,
        profile_html=profile_top_card_html(ZEPHYRINE, **profile),
        bubble=Bubble(ZEPHYRINE, None, html=html),
    )


async def test_an_h2_only_profile_checks_the_chip_by_the_top_cards_h2(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="netkeeper.linkedin.browser"):
        ran = await prefill(_h2_site())
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is True
    assert "prefill: the profile's name is read from the top card's one h2" in caplog.text
    assert ZEPHYRINE.name.casefold() not in caplog.text.casefold()


async def test_an_h2_only_profile_with_a_chip_that_differs_refuses() -> None:
    ran = await prefill(_h2_site("Somebody Else"))
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "recipient_name_mismatch"


async def test_a_credential_suffix_is_compared_whole() -> None:
    """The chip holds the whole name, the suffix too (the "..." is CSS only): equal
    passes, and neither side is cut or stripped to make them equal."""
    ran = await prefill(_h2_site(MBA, name=MBA))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is True
    for chip, name in ((ZEPHYRINE.name, MBA), (MBA, ZEPHYRINE.name), (f"{ZEPHYRINE.name}…", MBA)):
        ran = await prefill(_h2_site(chip, name=name))
        assert_no_keys(ran)
        assert ran.result.outcome.reason == "recipient_name_mismatch", (chip, name)


async def test_pronouns_in_a_sibling_span_are_not_part_of_the_name() -> None:
    ran = await prefill(_h2_site(pronouns="they/them"))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is True


async def test_readable_text_inside_the_h2_refuses_and_hidden_parts_do_not() -> None:
    """The gap the issue leaves open: a badge or pronouns with readable text inside the
    name's h2 refuse (safe); an aria-hidden icon or span is left out of the name."""
    readable = await prefill(_h2_site(in_heading=" <span>(they/them)</span>"))
    assert_no_keys(readable)
    assert readable.result.outcome.reason == "recipient_name_mismatch"
    for hidden in (
        ' <svg aria-hidden="true"></svg>',
        ' <span aria-hidden="true">(they/them)</span>',
    ):
        ran = await prefill(_h2_site(in_heading=hidden))
        assert ran.kind is MessageOutcomeKind.PREFILLED, (hidden, ran.result)
        assert ran.result.outcome.recipient_name_checked is True


@pytest.mark.parametrize(
    "profile",
    [
        {"top_card_h2s": 2},
        {"top_card_h2s": 0},
        {"contact_info": None},
        {"contact_info": f"/in/{THADDEUS.slug}/overlay/contact-info/"},
        {"contact_info": f"/in/{ZEPHYRINE.slug}/"},
    ],
    ids=["two h2s", "no h2", "no Contact info", "Contact info elsewhere", "not the overlay"],
)
async def test_no_single_top_card_h2_skips_the_name_check_and_the_card_decides(
    profile: dict[str, Any],
) -> None:
    """Without one name heading the check is skipped, as before, whatever the chip says;
    and the card still binds the recipient."""
    ran = await prefill(_h2_site("Somebody Else", **profile))
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is False
    site = _h2_site("Somebody Else", **profile)
    assert site.bubble is not None and site.bubble.html is not None
    site.bubble.html = site.bubble.html.replace(f"/in/{ZEPHYRINE.slug}/", f"/in/{THADDEUS.slug}/")
    assert_no_keys(await prefill(site))


async def test_two_contact_info_links_skip_the_name_check() -> None:
    """The second link is hidden, so only a count that includes hidden links sees two
    (#566: this kills the ``include_hidden=False`` mutant)."""
    site = _h2_site("Somebody Else")
    site.before = f'<a href="/in/{ZEPHYRINE.slug}/overlay/contact-info/" hidden>Contact info</a>'
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is False


def _one_section_h2(site: MessagingSite) -> MessagingSite:
    """``site`` with its Activity section gone, so its About title is the page's only
    ``h2`` after the top card."""
    site.profile_html = site.profile_html.replace("<section><h2>Activity</h2></section>", "")
    return site


@pytest.mark.parametrize(
    "shape",
    ["no h2 in the top card", "no h2 in the top card, two section titles", "an h2 after"],
)
async def test_an_h2_after_the_contact_info_link_is_not_the_name(
    shape: str, caplog: pytest.LogCaptureFixture
) -> None:
    """#566: the name's ``h2`` comes before the Contact info link. A top card with no
    ``h2`` lets the climb reach a section title ("About"); that, or an ``h2`` after the
    link, is no name heading: the check is skipped and the card decides, rather than
    refusing the right person by a section title."""
    if shape == "no h2 in the top card":
        site = _one_section_h2(_h2_site(top_card_h2s=0))
    elif shape == "no h2 in the top card, two section titles":
        site = _h2_site(top_card_h2s=0)
    else:
        site = _one_section_h2(_h2_site(top_card_h2s=0))
        site.profile_html = site.profile_html.replace(
            "</span></a>", f"</span></a><h2>{ZEPHYRINE.name}</h2>", 1
        )
    with caplog.at_level("DEBUG", logger="netkeeper.linkedin.browser"):
        ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is False
    assert "prefill: no name heading (no h2 before the Contact info link)" in caplog.text


async def test_more_than_one_h2_before_the_contact_info_link_has_its_own_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("DEBUG", logger="netkeeper.linkedin.browser"):
        ran = await prefill(_h2_site(top_card_h2s=2))
    assert ran.result.outcome.recipient_name_checked is False
    assert "prefill: no name heading (the top card holds more than one h2)" in caplog.text
    assert "no h2 before the Contact info link" not in caplog.text


async def test_an_h1_still_wins_over_the_top_cards_h2() -> None:
    """One h1 on the page is the name heading, as before #552, whatever the h2 says."""
    site = _h2_site(MBA, name=MBA)
    site.profile_html = f"<h1>{ZEPHYRINE.name}</h1>" + site.profile_html
    ran = await prefill(site)
    assert_no_keys(ran)
    assert ran.result.outcome.reason == "recipient_name_mismatch"


async def test_two_h1s_skip_even_with_a_top_card_h2() -> None:
    site = _h2_site("Somebody Else")
    site.profile_html = "<h1>A</h1><h1>B</h1>" + site.profile_html
    ran = await prefill(site)
    assert ran.kind is MessageOutcomeKind.PREFILLED, ran.result
    assert ran.result.outcome.recipient_name_checked is False
