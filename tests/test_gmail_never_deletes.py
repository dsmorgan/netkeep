"""netkeep deletes and trashes nothing in Gmail (ADR 0003, ADR 0009; #279).

The OAuth scope does not enforce this: ``gmail.modify`` allows ``drafts.delete``, and
``messages.modify`` could add ``TRASH``. The code does, so these tests pin it:

- **Statically**, over ``netkeep/``: no call to a delete, trash, or untrash method of
  the Gmail API (or a batch or thread modify, which could trash in bulk), no string naming
  one, and every ``modify_labels`` call adds only, never ``TRASH`` or ``SPAM``, from the one
  call site reviewed for it. Every Gmail API request the client builds is pinned by
  resource and method (so a resource held in a variable, or a method read by
  ``getattr``, fails), its one ``messages().modify`` call is pinned word for word, and a
  ``TRASH`` or ``SPAM`` string appears only where it is reviewed (#703).
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
import pytest
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

SENDER = PACKAGE / "services" / "campaign_sender.py"

REVIEWED_ADDS = frozenset({(SENDER, "(label_id,)")})
"""#723 review: an ``add=`` that names a label by a variable, by (file, expression as
written). The scan can't read a variable's value, so each is reviewed: the sender's
``label_id`` is the id Gmail gave the campaign's own label (``ensure_label``). Any
other ``add=`` may hold only string literals."""

GMAIL = PACKAGE / "campaigns" / "gmail.py"

#: #703: the ``users`` collection's sub-resources, each read as ``users.<name>()``.
RESOURCES = frozenset({"drafts", "history", "labels", "messages", "settings", "threads"})

#: #703: each Gmail API request the package builds, by (file, resource, method): count.
#: ``users`` is a method read on the collection itself (``users.getProfile``).
#: ``<held>`` would be a resource (or the collection) kept, passed, or read by anything
#: but a method call; ``<bound>`` is the collection's one assignment; ``<getattr>`` a
#: resource or method read by name. Each change here is reviewed against ADR 0009.
ALLOWED_API_CALLS = {
    (GMAIL, "service", "users"): 1,
    (GMAIL, "users", "<bound>"): 1,
    (GMAIL, "users", "getProfile"): 1,
    (GMAIL, "messages", "send"): 1,
    (GMAIL, "drafts", "create"): 1,
    (GMAIL, "drafts", "get"): 1,
    (GMAIL, "drafts", "list"): 1,
    (GMAIL, "messages", "get"): 1,
    (GMAIL, "threads", "get"): 1,
    (GMAIL, "messages", "list"): 1,
    (GMAIL, "labels", "list"): 1,
    (GMAIL, "labels", "create"): 1,
    (GMAIL, "messages", "modify"): 1,
    (GMAIL, "history", "list"): 1,
}

#: #703: the one label change, word for word: only ``modify_labels``'s own arguments,
#: so nothing (``TRASH`` included) is added to its body here.
MODIFY_CALL = (
    "self._users.messages().modify(userId='me', id=message_id,"
    " body={'addLabelIds': list(add), 'removeLabelIds': list(remove)})"
)

#: #703: where a ``TRASH`` or ``SPAM`` string (any case) may appear, by (file, value):
#: count. Only the fake's: its system labels, its search leaving both out, and its
#: refusal of ``label:spam`` and ``label:trash``.
ALLOWED_FORBIDDEN_LABEL_STRINGS = {
    (FAKE, "SPAM"): 2,
    (FAKE, "TRASH"): 2,
    (FAKE, "spam"): 1,
    (FAKE, "trash"): 1,
}

#: #703: methods of the API with no other meaning: read at all (held, or by ``getattr``),
#: each is a finding.
FORBIDDEN_ONLY_NAMES = frozenset({"trash", "untrash", "batchDelete", "batchModify"})


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


def _getattr_name(node: ast.AST) -> str | None:
    """The literal name ``getattr(x, "name")`` reads, if ``node`` is such a call."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return None


def scan_deletes(source: str, path: Path = MEMORY) -> list[Finding]:
    """Calls such as ``users.drafts().delete(...)``, strings such as ``"messages.trash"``,
    and (#703) any read of a trash-only method (``m.trash``, even held) or a ``getattr``
    of any forbidden method by name."""
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
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ONLY_NAMES:
            found.append(Finding(path, node.lineno, f"reads .{node.attr}"))
        elif (name := _getattr_name(node)) is not None and any(
            name in methods for methods in FORBIDDEN.values()
        ):
            found.append(Finding(path, getattr(node, "lineno", 0), f"getattr {name!r}"))
    return found


def api_calls(source: str, path: Path = MEMORY) -> dict[tuple[Path, str, str], int]:
    """#703: each Gmail API request built in ``source``, as :data:`ALLOWED_API_CALLS`
    keys it: a no-argument ``.<resource>()`` call and the method called on it, the
    ``_users`` collection's reads, ``.users()``, and a ``getattr`` of either by name."""
    tree = ast.parse(source)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    found: dict[tuple[Path, str, str], int] = {}

    def add(resource: str, method: str) -> None:
        found[(path, resource, method)] = found.get((path, resource, method), 0) + 1

    def method_called_on(node: ast.AST) -> str | None:
        """``m`` when ``node`` is the receiver of a call ``node.m(...)``."""
        attribute = parents.get(id(node))
        if not isinstance(attribute, ast.Attribute) or attribute.value is not node:
            return None
        call = parents.get(id(attribute))
        if isinstance(call, ast.Call) and call.func is attribute:
            return attribute.attr
        return None

    def on_the_api(receiver: ast.expr) -> bool:
        """Whether ``receiver`` is the collection (``_users``) or the service."""
        return (
            isinstance(receiver, ast.Attribute) and receiver.attr in ("_users", "_service")
        ) or (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Attribute)
            and receiver.func.attr == "users"
        )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in RESOURCES | {"users"}
            and (not (node.args or node.keywords) or on_the_api(node.func.value))
        ):
            # #723 review: a resource called with arguments (``messages(*())``) on the
            # collection is keyed apart, so it never stands in for a reviewed request.
            argued = "<arguments>" if node.args or node.keywords else ""
            resource = node.func.attr
            if resource == "users":
                add("service", f"users{argued}")
                continue
            add(resource, f"{method_called_on(node) or '<held>'}{argued}")
        elif isinstance(node, ast.Attribute) and node.attr == "_users":
            if isinstance(node.ctx, ast.Store):
                add("users", "<bound>")
                continue
            outer = parents.get(id(node))
            if (
                isinstance(outer, ast.Attribute)
                and outer.attr in RESOURCES
                and isinstance(parents.get(id(outer)), ast.Call)
            ):
                continue  # ``_users.<resource>()``, read above
            add("users", method_called_on(node) or "<held>")
        elif (name := _getattr_name(node)) in RESOURCES | {"users", "_users", "_service"}:
            add("<getattr>", str(name))
    return found


@cache
def _all_api_calls() -> tuple[tuple[tuple[Path, str, str], int], ...]:
    found: dict[tuple[Path, str, str], int] = {}
    for path, source in package_sources():
        found.update(api_calls(source, path))
    return tuple(found.items())


def all_api_calls() -> dict[tuple[Path, str, str], int]:
    return dict(_all_api_calls())


def modify_calls(source: str) -> list[str]:
    """#703: each ``<resource>().modify(...)`` call in ``source``, as written."""
    return [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "modify"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Attribute)
        and node.func.value.func.attr in RESOURCES
    ]


def forbidden_label_strings(source: str, path: Path = MEMORY) -> dict[tuple[Path, str], int]:
    """#703: each string that is ``TRASH`` or ``SPAM`` in any case, by (file, value)."""
    found: dict[tuple[Path, str], int] = {}
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.upper() in FORBIDDEN_LABELS
        ):
            found[(path, node.value)] = found.get((path, node.value), 0) + 1
    return found


def _is_empty(node: ast.expr) -> bool:
    return isinstance(node, ast.Tuple | ast.List | ast.Set) and not node.elts


def scan_modify_labels(source: str, path: Path = MEMORY) -> tuple[list[Finding], list[Finding]]:
    """``(calls, problems)``: every ``modify_labels`` call, and each that may remove a
    label or adds ``TRASH`` or ``SPAM``. Since #703 ``add=`` must be a literal tuple or
    list of strings (or one of :data:`REVIEWED_ADDS`), and a ``modify_labels`` read any
    other way (held, or by ``getattr``) is a call whose labels can't be read, so a
    problem too."""
    calls: list[Finding] = []
    problems: list[Finding] = []
    tree = ast.parse(source)
    called = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "modify_labels"
            and id(node) not in called
        ) or _getattr_name(node) == "modify_labels":
            line = getattr(node, "lineno", 0)
            calls.append(Finding(path, line, "modify_labels"))
            problems.append(Finding(path, line, "modify_labels read, not called"))
            continue
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
            elif keyword.arg == "add" and not (
                isinstance(keyword.value, ast.Tuple | ast.List)
                and all(
                    (isinstance(n, ast.Constant) and isinstance(n.value, str))
                    or (
                        isinstance(n, ast.Name)
                        and (path, ast.unparse(keyword.value)) in REVIEWED_ADDS
                    )
                    for n in keyword.value.elts
                )
            ):
                problems.append(Finding(path, node.lineno, "modify_labels adds unread labels"))
            elif keyword.arg == "add":
                labels = {
                    n.value
                    for n in ast.walk(keyword.value)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and n.value.upper() in FORBIDDEN_LABELS
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


@pytest.mark.parametrize(
    "snippet",
    [
        # #703: a held resource, and a method read by name.
        "m = users.messages()\nm.trash(userId='me', id=x).execute()\n",
        "m = users.messages()\nm.batchDelete(userId='me', body=b).execute()\n",
        "t = users.threads()\nt.untrash(userId='me', id=x).execute()\n",
        "send = users.messages().batchModify\n",
        "getattr(users.messages(), 'trash')(userId='me', id=x)\n",
        "getattr(users.drafts(), 'delete')(userId='me', id=x)\n",
        "getattr(users.threads(), 'modify')(userId='me', id=x, body=b)\n",
    ],
)
def test_the_delete_scanner_catches_a_held_or_named_method(snippet: str) -> None:
    assert scan_deletes(snippet), snippet


@pytest.mark.parametrize(
    "line",
    [
        # #703: a held resource, whatever is called on it later.
        "    def _held(self):\n        d = self._users.drafts()\n        return d\n",
        "    def _held(self):\n        return self._users.drafts\n",
        "    def _held(self):\n        return self._users\n",
        # A method read by name, on a resource or on the collection.
        "    def _held(self):\n        return getattr(self._users, 'messages')()\n",
        "    def _held(self):\n        return getattr(self, '_users')\n",
        "    def _held(self):\n        return getattr(self._service, 'users')()\n",
        # A new method, a second call, or a new resource.
        "    def _held(self, i):\n        return self._users.drafts().delete(userId='me', id=i)\n",
        "    def _held(self, b):\n        return self._users.drafts().send(userId='me', body=b)\n",
        "    def _held(self):\n        return self._users.settings().filters()\n",
        "    def _held(self):\n        return self._users.stop(userId='me')\n",
        "    def _held(self):\n        return self._service.users().messages().get(id=1)\n",
        # #723 review: a resource or the collection called with arguments.
        "    def _held(self, b):\n        return self._users.messages(*()).insert(body=b)\n",
        "    def _held(self, b):\n        return self._users.drafts(**{}).send(body=b)\n",
        "    def _held(self):\n        return self._service.users(**{})\n",
    ],
)
def test_the_api_call_pin_catches_a_new_request(line: str) -> None:
    source = GMAIL.read_text(encoding="utf-8")
    anchor = "    # --- plumbing"
    assert anchor in source
    mutated = source.replace(anchor, line + "\n" + anchor, 1)
    found = {k: v for k, v in all_api_calls().items() if k[0] != GMAIL}
    found |= api_calls(mutated, GMAIL)
    assert found != ALLOWED_API_CALLS


def test_the_api_call_pin_catches_one_elsewhere() -> None:
    found = all_api_calls() | api_calls("def f(gmail):\n    return gmail._users\n")
    assert found != ALLOWED_API_CALLS


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"addLabelIds": list(add)', '"addLabelIds": [*add, "TRASH"]'),
        ('"addLabelIds": list(add)', '"addLabelIds": list(add) + [TRASH]'),
        ('"removeLabelIds": list(remove)', '"removeLabelIds": ["INBOX"]'),
        ('body={"addLabelIds"', '**extra, body={"addLabelIds"'),
    ],
)
def test_the_modify_pin_catches_a_changed_body(old: str, new: str) -> None:
    source = GMAIL.read_text(encoding="utf-8")
    assert source.count(old) == 1
    assert modify_calls(source.replace(old, new)) != [MODIFY_CALL]


def test_the_label_string_scan_catches_a_new_one() -> None:
    for snippet in ('TRASH = "TRASH"\n', 'x = ("Spam",)\n', 'q = "trash"\n'):
        assert forbidden_label_strings(snippet), snippet
    assert forbidden_label_strings('q = "in:trash"\n') == {}


def test_the_label_scanner_catches_a_remove_and_a_trash() -> None:
    _, problems = scan_modify_labels('g.modify_labels(m, remove=("INBOX",), purpose=p)\n')
    assert problems
    _, problems = scan_modify_labels('g.modify_labels(m, add=("TRASH",), purpose=p)\n')
    assert problems
    _, problems = scan_modify_labels('g.modify_labels(m, add=[label, "SPAM"], purpose=p)\n')
    assert problems
    _, problems = scan_modify_labels("g.modify_labels(m, **changes)\n")
    assert problems
    reviewed = "g.modify_labels(m, add=(label_id,), purpose=p)\n"
    calls, problems = scan_modify_labels(reviewed, SENDER)
    assert (len(calls), problems) == (1, [])
    calls, problems = scan_modify_labels('g.modify_labels(m, add=("Label_1",), purpose=p)\n')
    assert (len(calls), problems) == (1, [])
    # #723 review: the reviewed expression only in its own file, and only as written.
    for snippet, path in (
        (reviewed, MEMORY),
        (reviewed, PACKAGE / "services" / "campaign_replies.py"),
        ("g.modify_labels(m, add=(trash_id,), purpose=p)\n", SENDER),
        ("g.modify_labels(m, add=(label_id, other), purpose=p)\n", SENDER),
    ):
        _, problems = scan_modify_labels(snippet, path)
        assert problems, (snippet, path)


@pytest.mark.parametrize(
    "snippet",
    [
        # #703: labels the scan can't read, a lowercase trash, and a held method.
        "g.modify_labels(m, add=labels, purpose=p)\n",
        "g.modify_labels(m, add=(*labels,), purpose=p)\n",
        "g.modify_labels(m, add=[label for label in labels], purpose=p)\n",
        "g.modify_labels(m, add=(pick(),), purpose=p)\n",
        'g.modify_labels(m, add=("trash",), purpose=p)\n',
        "change = g.modify_labels\n",
        "change = functools.partial(g.modify_labels, m)\n",
        "getattr(g, 'modify_labels')(m, add=('TRASH',), purpose=p)\n",
    ],
)
def test_the_label_scanner_catches_what_it_cant_read(snippet: str) -> None:
    calls, problems = scan_modify_labels(snippet)
    assert calls and problems, snippet


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


def test_only_the_reviewed_gmail_api_requests_are_built() -> None:
    """#703: every request, by resource and method, from the one client."""
    assert all_api_calls() == ALLOWED_API_CALLS


def test_the_one_label_change_is_the_reviewed_one() -> None:
    """#703: ``messages().modify`` sends exactly ``modify_labels``'s labels."""
    calls = [call for path, source in package_sources() for call in modify_calls(source)]
    assert calls == [MODIFY_CALL]


def test_trash_and_spam_are_named_only_where_reviewed() -> None:
    found: dict[tuple[Path, str], int] = {}
    for path, source in package_sources():
        found |= forbidden_label_strings(source, path)
    assert found == ALLOWED_FORBIDDEN_LABEL_STRINGS


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
    # #723 re-review: the reviewed ``add=(label_id,)`` is one call, not a pattern.
    calls, _ = scan_modify_labels(dict(package_sources())[SENDER], SENDER)
    assert len(calls) == 1


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
