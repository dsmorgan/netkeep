"""Spelling warnings for a template's literal text (#522, decided on #493).

What this module decides
------------------------
- **Spelling only, warnings only.** Each word that may be misspelled is a
  :attr:`~netkeeper.campaigns.render.LintRule.SPELLING` warning. A warning never blocks
  a save or an activation; grammar is left for later.
- **Local only.** The word list is ``pyspellchecker``'s English frequency list, which
  ships inside the package and is read from it once per process. Nothing is fetched.
- **The template, not a rendered message.** Only the literal text is read
  (:func:`~netkeeper.campaigns.render.literal_text`): a merge field and anything inside
  ``{{ }}``, ``{% %}`` or ``{# #}`` is never checked. URLs, email addresses, dotted names
  (``example.com``, ``e.g.``), code spans in backticks, words in all caps, and words with
  a digit or underscore are skipped too. A hyphenated word is checked part by part.
- **The dictionary is the caller's.** :func:`spelling_issues` takes the words to accept
  on top of the list: the user dictionary
  (:mod:`netkeeper.services.user_dictionary`). Comparison ignores case.
- **Suggestions** are the known words one edit away, most common first, up to
  :data:`MAX_SUGGESTIONS`. Two edits away is left out: for a long word with no near
  match it takes about half a second, and the editor lints as you type.
- **Each word once per part,** on the first line it appears, and at most
  :data:`MAX_FINDINGS_PER_PART` of them; one more warning counts the rest.

Nothing here logs: a template's words and the user's dictionary are both private.
"""

from __future__ import annotations

import bisect
import functools
import re
from collections.abc import Container, Iterator
from typing import Final

from spellchecker import SpellChecker

from netkeeper.campaigns.render import LintIssue, LintRule, Part, Severity, literal_text
from netkeeper.models import TemplateChannel

MAX_SUGGESTIONS: Final = 3
MAX_FINDINGS_PER_PART: Final = 50

# Blanked out before words are found, so nothing in them is checked. Each is replaced by
# spaces of the same length, which keeps every later offset, and so every line, right.
_SKIPPED: Final = re.compile(
    r"`[^`\r\n]*`"  # a code span
    r"|(?:\b[a-z][a-z0-9+.-]*://|\bhttps?:|\bmailto:|\bwww\.)\S*"  # a URL, broken ones too
    r"|\S*@\S*"  # an email address (or an @handle)
    r"|\S*\w\.\w\S*",  # a dotted name: example.com, e.g., v1.2
    re.IGNORECASE,
)
# A run of letters, digits, apostrophes and hyphens: one word, or a hyphenated compound.
_TOKEN: Final = re.compile(r"[\w'\u2019-]+")
_LINE_BREAK: Final = re.compile(r"\r\n|\r|\n")
_EDGES: Final = "'\u2019-"


@functools.cache
def _checker() -> SpellChecker:
    """The English word list, loaded from the installed package on first use (about
    0.1 s), then kept for the process."""
    return SpellChecker(language="en", distance=1)


def normalize(word: str) -> str:
    """How a word is compared: lowercased, with a curly apostrophe made straight."""
    return word.replace("\u2019", "'").strip(_EDGES).lower()


def words(text: str) -> Iterator[tuple[int, str]]:
    """Each word of ``text`` the check would read, with its offset, as written.

    Also how the user dictionary splits a contact's name or company into words.
    """
    blanked = _SKIPPED.sub(lambda match: " " * len(match.group()), text)
    for match in _TOKEN.finditer(blanked):
        token = match.group()
        if any(char.isdigit() or char == "_" for char in token):
            continue
        offset = match.start()
        for part in token.split("-"):
            stripped = part.strip("'\u2019")
            start = offset + part.find(stripped) if stripped else offset
            offset += len(part) + 1
            if stripped.endswith(("'s", "\u2019s")):
                stripped = stripped[:-2]
            if len(stripped) < 2 or stripped.isupper():
                continue
            yield start, stripped


def _line_starts(text: str) -> list[int]:
    return [match.end() for match in _LINE_BREAK.finditer(text)]


def _suggestions(word: str) -> list[str]:
    checker = _checker()
    lowered = normalize(word)
    found = checker.known(checker.edit_distance_1(lowered))
    found.discard(lowered)
    ranked = sorted(found, key=lambda known: (-checker[known], known))[:MAX_SUGGESTIONS]
    if word[:1].isupper():
        ranked = [known[:1].upper() + known[1:] for known in ranked]
    return ranked


def _message(word: str, suggestions: list[str]) -> str:
    if not suggestions:
        return f'"{word}" may be misspelled'
    return f'"{word}" may be misspelled; did you mean {", ".join(suggestions)}?'


def _part_issues(source: str, part: Part, dictionary: Container[str]) -> list[LintIssue]:
    pieces = literal_text(source)
    if pieces is None:
        return []  # does not lex: lint already has a syntax error for it
    checker = _checker()
    known = checker.word_frequency.dictionary
    seen: set[str] = set()
    issues: list[LintIssue] = []
    more = 0
    for first_line, piece in pieces:
        starts: list[int] | None = None
        for offset, word in words(piece):
            lowered = normalize(word)
            if lowered in seen or lowered in known or lowered in dictionary:
                continue
            seen.add(lowered)
            if len(issues) >= MAX_FINDINGS_PER_PART:
                more += 1
                continue
            if starts is None:
                starts = _line_starts(piece)
            line = first_line + bisect.bisect_right(starts, offset)
            issues.append(
                LintIssue(
                    LintRule.SPELLING,
                    Severity.WARNING,
                    part,
                    _message(word, _suggestions(word)),
                    field=word,
                    line=line,
                )
            )
    if more:
        issues.append(
            LintIssue(
                LintRule.SPELLING,
                Severity.WARNING,
                part,
                f"{more} more {'word' if more == 1 else 'words'} may be misspelled; "
                f"only the first {MAX_FINDINGS_PER_PART} are listed",
            )
        )
    return issues


def spelling_issues(
    channel: TemplateChannel,
    subject: str | None,
    body: str,
    dictionary: Container[str],
) -> list[LintIssue]:
    """A warning for each word of the template's literal text that may be misspelled.

    ``dictionary`` holds more words to accept, each as :func:`normalize` gives it. A
    LinkedIn template's subject is never checked: it is ignored, as lint says (#448).
    """
    issues: list[LintIssue] = []
    if subject is not None and channel is TemplateChannel.EMAIL:
        issues.extend(_part_issues(subject, Part.SUBJECT, dictionary))
    issues.extend(_part_issues(body, Part.BODY, dictionary))
    return issues
