"""The user dictionary: words the template spelling check accepts for one user (#522).

What this module decides
------------------------
- **Stored as one setting.** The words you add are a JSON list in ``settings_kv`` under
  :data:`SETTING_KEY`, per user, so the dictionary needs no table and no migration.
- **Seeded words are read, not copied.** :func:`known_words` adds, each time it runs,
  the built-in list (:data:`BUILT_IN_WORDS`), your own name, and the first, last and
  preferred names and companies of all your contacts, archived ones included. A contact
  added tomorrow is in tomorrow's dictionary, and nothing has to be kept in step with
  the contacts. :func:`added_words` lists only what you added, and only an added word
  can be removed.
- **Words are compared as** :func:`netkeeper.campaigns.spelling.normalize` gives them:
  lowercased, with a curly apostrophe made straight.
- **Never logged.** A dictionary word is often a person's or a company's name, so no
  function here logs a word, and errors name the problem, not the word.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.campaigns.spelling import normalize, words
from netkeeper.models import Contact, User
from netkeeper.scoping import scoped
from netkeeper.services.settings_kv import get_setting, set_setting

SETTING_KEY: Final = "templates.dictionary"

BUILT_IN_WORDS: Final = frozenset({"linkedin", "inmail", "netkeeper"})
"""Words every user's dictionary has: the ones a networking message names most."""

WORD_MAX_LENGTH: Final = 64
MAX_WORDS: Final = 5_000
"""How many words you can add. Far past a real dictionary; it keeps the setting small."""

# Letters, with apostrophes and hyphens inside: what the spelling check reads as a word.
_WORD: Final = re.compile(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*")


class InvalidWord(ValueError):
    """A word the dictionary refuses: empty, too long, or not a word the check reads."""


class DictionaryFull(ValueError):
    """Adding the words would take the dictionary past :data:`MAX_WORDS`."""


def clean_word(word: str) -> str:
    """``word`` as the dictionary stores it, or :class:`InvalidWord`."""
    stripped = word.strip()
    if not stripped:
        raise InvalidWord("a dictionary word can't be empty")
    if len(stripped) > WORD_MAX_LENGTH:
        raise InvalidWord(f"a dictionary word can be at most {WORD_MAX_LENGTH} characters")
    if _WORD.fullmatch(stripped) is None:
        raise InvalidWord(
            "a dictionary word is letters only, with an apostrophe or hyphen inside it: "
            "no spaces, digits or other characters"
        )
    return normalize(stripped)


def added_words(session: Session, user: User) -> list[str]:
    """The words ``user`` added, sorted. A malformed row (not JSON, not a list) reads as
    none, and the items in it that are not strings are ignored."""
    try:
        stored = get_setting(session, user, SETTING_KEY, [])
    except ValueError:  # a row that is not JSON: treated as no added words
        return []
    if not isinstance(stored, list):
        return []
    return sorted({item for item in stored if isinstance(item, str)})


def add_words(session: Session, user: User, new: Iterable[str]) -> list[str]:
    """Add ``new`` to ``user``'s dictionary; returns the words actually added (not there
    before), cleaned. Needs a writer session."""
    cleaned = [clean_word(word) for word in new]
    current = added_words(session, user)
    have = set(current)
    added = sorted({word for word in cleaned if word not in have})
    if not added:
        return []
    if len(have) + len(added) > MAX_WORDS:
        raise DictionaryFull(f"the dictionary holds at most {MAX_WORDS:,} words")
    set_setting(session, user, SETTING_KEY, sorted(have.union(added)))
    return added


def remove_words(session: Session, user: User, old: Iterable[str]) -> list[str]:
    """Remove ``old`` from ``user``'s added words; returns the ones that were there.
    A seeded word (a contact's name, a built-in word) is not stored, so it stays. Needs
    a writer session."""
    wanted = {normalize(word) for word in old}
    current = added_words(session, user)
    removed = sorted(wanted.intersection(current))
    if removed:
        set_setting(session, user, SETTING_KEY, [word for word in current if word not in wanted])
    return removed


def _name_words(texts: Iterable[str | None]) -> set[str]:
    out: set[str] = set()
    for text in texts:
        if text:
            out.update(normalize(word) for _, word in words(text))
    return out


def seeded_words(session: Session, user: User) -> set[str]:
    """The words every check accepts without your adding them: the built-in list, your
    name, and your contacts' names and companies."""
    statement = scoped(user, Contact).with_only_columns(
        Contact.first_name, Contact.last_name, Contact.preferred_name, Contact.current_company
    )
    seeded = set(BUILT_IN_WORDS)
    seeded.update(_name_words([user.display_name]))
    for row in session.execute(statement):
        seeded.update(_name_words(row))
    return seeded


def known_words(session: Session, user: User) -> frozenset[str]:
    """Every word the spelling check accepts for ``user`` beyond the English list."""
    return frozenset(seeded_words(session, user).union(added_words(session, user)))
