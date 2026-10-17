"""An open Contact info dialog is never a Messaging bubble (#558, from the CP8d capture).

The dialog is a ``<dialog>`` with no ``role`` attribute, named ``Contact info`` through
``aria-labelledby`` -> ``header#dialog-header``, and modal while open. The bubble checks look
for ``Messaging`` dialogs, so it must not count, and a profile left on it must not read as a
bubble. The page is invented ("Quorbelle Fictionary"); only the structure is the capture's.
"""

from __future__ import annotations

import messaging_dom
import pytest
from messaging_dom import MessagingSite, MessagingTab
from messaging_pages import OWNER, Member, existing_bubble_html
from run_fakes import fake_provider

from netkeeper.linkedin.enrich import LINKEDIN_ORIGIN
from netkeeper.linkedin.page_messaging import PageBubbleCheck

PERSON = Member(558, "Quorbelle", "Fictionary", "Invented headline for #558")
PROFILE = f"{LINKEDIN_ORIGIN}/in/{PERSON.slug}/overlay/contact-info/"

CONTACT_INFO_DIALOG = """
<dialog data-testid="dialog" aria-labelledby="dialog-header" open>
  <button type="button" aria-label="Dismiss"><svg aria-hidden="true"></svg></button>
  <div>
    <header id="dialog-header"><h2>Contact info</h2></header>
    <div data-testid="dialog-content"><p>Email</p><p>invented@example.invalid</p></div>
  </div>
  <section><h2 data-testid="toasts-title">Invented toast</h2></section>
</dialog>
"""


@pytest.fixture(autouse=True)
def dialog_tag_has_the_dialog_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser gives ``<dialog>`` the ``dialog`` role with no attribute; the fake DOM doesn't,
    and without this the guard would pass for the wrong reason (nothing is a dialog)."""
    real = messaging_dom.role_of

    def role_of(element: messaging_dom.Element) -> str | None:
        return "dialog" if element.tag == "dialog" else real(element)

    monkeypatch.setattr(messaging_dom, "role_of", role_of)


def _tab(site: MessagingSite, html: str, url: str = PROFILE) -> MessagingTab:
    tab = MessagingTab(site)
    tab._url = url
    tab.load(html)
    site.pages.append(tab)
    return tab


async def _bubble_open(site: MessagingSite, tab: MessagingTab) -> bool:
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        return await run._bubble_already_open(tab)  # type: ignore[arg-type]


async def test_the_fake_sees_the_contact_info_dialog_as_a_dialog() -> None:
    """The guard's premise: this page does hold a dialog, named Contact info."""
    site = MessagingSite(PERSON)
    tab = _tab(site, CONTACT_INFO_DIALOG)
    dialogs = tab.get_by_role("dialog", name="Contact info", exact=True, include_hidden=True)
    assert await dialogs.count() == 1


async def test_an_open_contact_info_dialog_is_not_an_open_bubble() -> None:
    site = MessagingSite(PERSON)
    tab = _tab(site, f"<main><h2>Profile</h2>{CONTACT_INFO_DIALOG}</main>")
    assert await _bubble_open(site, tab) is False


async def test_a_real_bubble_beside_the_dialog_is_still_counted() -> None:
    """The guard doesn't blind the check: a Messaging bubble on the same page still counts."""
    site = MessagingSite(PERSON)
    html = CONTACT_INFO_DIALOG + existing_bubble_html(PERSON, owner=OWNER)
    tab = _tab(site, html)
    assert await _bubble_open(site, tab) is True


async def test_the_bubble_check_counts_no_dialog_on_the_contact_info_overlay() -> None:
    site = MessagingSite(PERSON)
    _tab(site, CONTACT_INFO_DIALOG)
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        found = await PageBubbleCheck(run).run(PERSON.profile_id)
    assert (found.on_origin, found.with_bubble, found.for_contact) == (1, 0, 0)
    assert found.dialogs is None and found.shape is None
