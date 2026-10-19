"""netkeep deletes and trashes nothing in Gmail (ADR 0003, ADR 0009; #279).

The OAuth scope does not enforce this: ``gmail.modify`` allows ``drafts.delete``, and
``messages.modify`` could add ``TRASH``. The code does, so these tests pin it:

- **Statically**, over ``netkeep/``: no call to a delete, trash, or untrash method of
  the Gmail API (or a batch or thread modify, which could trash in bulk), no string naming
  one, and every ``modify_labels`` call adds only, never ``TRASH`` or ``SPAM``, from the one
  call site reviewed for it.
- **At run time**, through :class:`~netkeep.campaigns.gmail_fake.FakeGmail`: a draft
  campaign's life (drafted, sent by hand, replied to, a draft discarded by hand) makes no
  such call, and each label change only adds.

The fake's helpers that act as the person (sending, scheduling, or discarding a draft in
Gmail) are excluded by name: they stand for what you do in Gmail, not what netkeep does.
Each scanner is shown a snippet it must catch, so a scanner that matches nothing fails.
"""

from __future__ import annotations

import ast
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import factories
from campaign_fakes import ARMED_FOR_SEND, NOW, SETTINGS, make_mailbox
from sqlalchemy.orm import Session, sessionmaker

from netkeep.campaigns import gmail as gmail_module
from netkeep.campaigns.gmail_fake import FakeGmail
from netkeep.db import session_scope
from netkeep.models import Message, MessageStatus, StepMode, TemplateChannel, User
from netkeep.scoping import scoped
from netkeep.services.campaign_engine import run_tick
from netkeep.services.campaign_sender import GmailSender

PACKAGE = Path(__file__).resolve().parents[1] / "netkeep"
FAKE = PACKAGE / "campaigns" / "gmail_fake.py"
MEMORY = Path("<memory>")

#: Gmail API resources, and the methods of each that delete, trash, or untrash, or that
#: change labels in bulk or on a whole thread (either could add ``TRASH``).
FORBIDDEN: dict[str, frozenset[str]] = {
    "drafts": frozenset({"delete"}),
    "messages": frozenset({"delete", "trash", "untrash", "batchDelete", "batchModify"}),
    "threads": frozenset({"delete", "trash", "untrash", "modify"}),
}
FORBIDDEN_NAMES = frozenset(f"{r}.{m}" for r, methods in FORBIDDEN.items() for m in methods)
"""The same methods as a ``purpose`` log or ``_run`` would name them."""

FORBIDDEN_LABELS = frozenset({"TRASH", "SPAM"})

FAKE_PERSON_HELPERS = frozenset(
    {
        "send_draft",
        "schedule_draft",
        "send_scheduled",
        "cancel_scheduled",
        "discard_draft",
        "delete",
        "forget_history",
    }
)
"""FakeGmail methods that act as the person in Gmail, left out of the scan by name."""

MODIFY_LABELS_CALLERS = frozenset({"services/campaign_sender.py"})
"""Where ``modify_labels`` is called. A new caller is reviewed against ADR 0009 first."""


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


# --- the scanners ------------------------------------------------------------------


def _skipped(tree: ast.Module, path: Path) -> set[ast.AST]:
    """The bodies of the fake's person helpers, when ``path`` is the fake."""
    if path != FAKE:
        return set()
    skipped: set[ast.AST] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "FakeGmail":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in FAKE_PERSON_HELPERS:
                    skipped.update(ast.walk(item))
    return skipped


def scan_deletes(source: str, path: Path = MEMORY) -> list[Finding]:
    """Calls such as ``users.drafts().delete(...)``, and strings such as ``"messages.trash"``."""
    tree = ast.parse(source)
    skipped = _skipped(tree, path)
    found: list[Finding] = []
    for node in ast.walk(tree):
        if node in skipped:
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Attribute)
            and node.func.attr in FORBIDDEN.get(node.func.value.func.attr, frozenset())
        ):
            what = f"{node.func.value.func.attr}().{node.func.attr}()"
            found.append(Finding(path, node.lineno, what))
        elif isinstance(node, ast.Constant) and node.value in FORBIDDEN_NAMES:
            found.append(Finding(path, node.lineno, f"names {node.value}"))
    return found


def _is_empty(node: ast.expr) -> bool:
    return isinstance(node, ast.Tuple | ast.List | ast.Set) and not node.elts


def scan_modify_labels(source: str, path: Path = MEMORY) -> tuple[list[Finding], list[Finding]]:
    """``(calls, problems)``: every ``modify_labels`` call, and each that may remove a
    label or adds ``TRASH`` or ``SPAM``."""
    calls: list[Finding] = []
    problems: list[Finding] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "modify_labels"
        ):
            continue
        calls.append(Finding(path, node.lineno, "modify_labels"))
        for keyword in node.keywords:
            if keyword.arg is None:
                problems.append(Finding(path, node.lineno, "modify_labels(**...)"))
            elif keyword.arg == "remove" and not _is_empty(keyword.value):
                problems.append(Finding(path, node.lineno, "modify_labels removes a label"))
            elif keyword.arg == "add":
                labels = {
                    n.value
                    for n in ast.walk(keyword.value)
                    if isinstance(n, ast.Constant) and n.value in FORBIDDEN_LABELS
                }
                if labels:
                    what = f"modify_labels adds {', '.join(sorted(labels))}"
                    problems.append(Finding(path, node.lineno, what))
        if len(node.args) > 1:
            problems.append(Finding(path, node.lineno, "modify_labels with positional labels"))
    return calls, problems


@cache
def package_sources() -> tuple[tuple[Path, str], ...]:
    return tuple((path, path.read_text(encoding="utf-8")) for path in sorted(PACKAGE.rglob("*.py")))


def all_findings() -> Iterator[Finding]:
    for path, source in package_sources():
        yield from scan_deletes(source, path)


# --- the scanners catch what they must ---------------------------------------------


def test_the_delete_scanner_catches_each_forbidden_call() -> None:
    for resource, methods in FORBIDDEN.items():
        for method in sorted(methods):
            snippet = f'users.{resource}().{method}(userId="me", id=x).execute()\n'
            assert scan_deletes(snippet), snippet
            assert scan_deletes(f'run("{resource}.{method}", purpose, request)\n')
    assert scan_deletes('users.drafts().create(userId="me", body=b)\n') == []
    assert scan_deletes('users.messages().modify(userId="me", id=x, body=b)\n') == []


def test_the_label_scanner_catches_a_remove_and_a_trash() -> None:
    _, problems = scan_modify_labels('g.modify_labels(m, remove=("INBOX",), purpose=p)\n')
    assert problems
    _, problems = scan_modify_labels('g.modify_labels(m, add=("TRASH",), purpose=p)\n')
    assert problems
    _, problems = scan_modify_labels('g.modify_labels(m, add=[label, "SPAM"], purpose=p)\n')
    assert problems
    _, problems = scan_modify_labels("g.modify_labels(m, **changes)\n")
    assert problems
    calls, problems = scan_modify_labels("g.modify_labels(m, add=(label_id,), purpose=p)\n")
    assert (len(calls), problems) == (1, [])


def test_the_fake_still_has_each_excluded_helper() -> None:
    """A renamed helper would make the exclusion list stale, not wider."""
    for name in FAKE_PERSON_HELPERS:
        assert callable(getattr(FakeGmail, name, None)), name


def test_the_fake_scan_is_not_vacuous() -> None:
    """The fake is scanned too, apart from its person helpers."""
    assert any(path == FAKE for path, _ in package_sources())
    snippet = "class FakeGmail:\n    def profile(self):\n        users.drafts().delete(id=x)\n"
    assert scan_deletes(snippet, FAKE)


# --- the rules over netkeep/ -----------------------------------------------------


def test_nothing_in_netkeep_deletes_or_trashes_in_gmail() -> None:
    findings = [str(f) for f in all_findings()]
    assert findings == [], "netkeep deletes nothing in Gmail (ADR 0009):\n" + "\n".join(findings)


def test_modify_labels_only_adds_and_only_from_its_reviewed_caller() -> None:
    callers: set[str] = set()
    problems: list[str] = []
    for path, source in package_sources():
        calls, found = scan_modify_labels(source, path)
        if calls:
            callers.add(path.relative_to(PACKAGE).as_posix())
        problems += [str(f) for f in found]
    assert problems == []
    assert callers == MODIFY_LABELS_CALLERS


def test_the_gmail_interface_has_no_delete_or_trash() -> None:
    members = {
        name
        for name in dir(gmail_module.Gmail)
        if not name.startswith("_") and callable(getattr(gmail_module.Gmail, name))
    }
    assert members == {
        "profile",
        "send",
        "create_draft",
        "get_draft",
        "list_drafts",
        "get_message",
        "get_thread",
        "search",
        "list_labels",
        "create_label",
        "modify_labels",
        "history",
    }
    client = {name for name in vars(gmail_module.GmailClient) if not name.startswith("_")}
    assert client <= members


# --- at run time, through the fake -------------------------------------------------


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class LabelChange:
    add: tuple[str, ...]
    remove: tuple[str, ...]


class Watched(FakeGmail):
    """A FakeGmail that keeps each label change netkeep asks for."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.changes: list[LabelChange] = []

    def modify_labels(
        self,
        message_id: str,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
        purpose: str,
    ) -> None:
        self.changes.append(LabelChange(tuple(add), tuple(remove)))
        super().modify_labels(message_id, add=add, remove=remove, purpose=purpose)


def test_a_draft_campaigns_life_deletes_nothing(session_factory: sessionmaker[Session]) -> None:
    clock = Clock()
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, email="me@example.com", **ARMED_FOR_SEND)
        campaign = factories.make_campaign(
            session, user, channels=(TemplateChannel.EMAIL,) * 2, mailbox_id=mailbox.id
        )
        for step in campaign.steps:
            step.mode = StepMode.DRAFT
        for address in ("ada@example.test", "grace@example.test"):
            contact = factories.make_contact(session, user, emails=[address])
            factories.make_enrollment(session, campaign, contact, next_action_at=clock.now)
        user_id, mailbox_id = user.id, mailbox.id
    gmail = Watched("me@example.com", mailbox_id=mailbox_id, clock=clock)
    sender = GmailSender(
        session_factory,
        opener=lambda _user, _mailbox: gmail,
        clock=clock,
        drafts_every=timedelta(0),
        replies_every=timedelta(0),
    )

    def tick(at: timedelta) -> None:
        clock.now = NOW + at
        run_tick(
            session_factory, settings=SETTINGS, sender=sender, clock=clock, rng=random.Random(1)
        )

    for minutes in range(0, 60, 5):  # the mailbox spaces its drafts apart
        tick(timedelta(minutes=minutes))
    first, second = sorted(gmail.drafts())
    sent = gmail.send_draft(first, at=NOW + timedelta(hours=1))  # the person sends one
    tick(timedelta(hours=2))
    gmail.reply(sent, sender="ada@example.test", at=NOW + timedelta(hours=3))
    gmail.discard_draft(second)  # and deletes the other
    for hours in (4, 5, 6):
        tick(timedelta(hours=hours))

    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        statuses = {m.status for m in session.scalars(scoped(owner, Message))}
    assert {MessageStatus.SENT, MessageStatus.DISCARDED} <= statuses
    methods = {method for method, _ in gmail.calls}
    assert methods.isdisjoint(FORBIDDEN_NAMES)
    assert gmail.changes, "the scenario must change a label, or this test proves nothing"
    for change in gmail.changes:
        assert change.remove == ()
        assert FORBIDDEN_LABELS.isdisjoint(change.add)
