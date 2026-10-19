"""Spelling warnings for templates and the user dictionary (#522).

Every word, name and company here is invented. The checks are local: the word list
ships inside ``pyspellchecker``, and nothing here reaches a network.
"""

from __future__ import annotations

import logging
from pathlib import Path

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from time_limit import Stopwatch, scaled
from typer.testing import CliRunner

from netkeep import migrations
from netkeep.campaigns import spelling
from netkeep.campaigns import templates as template_service
from netkeep.campaigns.render import LintIssue, LintRule, Part, Severity
from netkeep.cli import app as cli
from netkeep.db import database_url, make_engine, make_session_factory, session_scope
from netkeep.models import TemplateChannel, User, UserKind
from netkeep.scoping import install_scope_guard
from netkeep.services import user_dictionary
from netkeep.services.settings_kv import set_setting
from netkeep.services.users import ensure_local_user

CSRF = {"X-Netkeep-Client": "1"}
EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN


def _check(
    body: str, subject: str | None = None, *, known: frozenset[str] = frozenset()
) -> list[LintIssue]:
    return spelling.spelling_issues(EMAIL, subject, body, known)


def _flagged(body: str, subject: str | None = None, **kwargs: frozenset[str]) -> list[str]:
    return [issue.field or "" for issue in _check(body, subject, **kwargs)]


# --- the check ------------------------------------------------------------------------


def test_a_misspelling_is_a_warning_with_its_line_and_suggestions() -> None:
    body = "Hi {{ first_name }},\n\nI hope you recieve this well.\nTalk soon"
    [issue] = _check(body)
    assert issue.rule is LintRule.SPELLING
    assert issue.severity is Severity.WARNING
    assert (issue.part, issue.field, issue.line) == (Part.BODY, "recieve", 3)
    assert issue.message == '"recieve" may be misspelled; did you mean receive, relieve?'


def test_the_subject_is_checked_and_suggestions_keep_a_capital() -> None:
    [issue] = _check("Hi {{ first_name }}", subject="Qestion for you")
    assert (issue.part, issue.field, issue.line) == (Part.SUBJECT, "Qestion", 1)
    assert "did you mean Question?" in issue.message


def test_at_most_three_suggestions_and_none_when_nothing_is_close() -> None:
    [teh] = _check("teh")
    assert teh.message.count(",") == 2  # three suggestions
    [far] = _check("Zqxvbwq")
    assert far.message == '"Zqxvbwq" may be misspelled'


def test_a_word_is_reported_once_on_its_first_line() -> None:
    assert [(i.field, i.line) for i in _check("recieve\nok\nrecieve Recieve")] == [("recieve", 1)]


def test_merge_fields_and_everything_inside_them_are_skipped() -> None:
    body = (
        "Hi {{ frist_nmae }}, {{ company | default('Acmezz Wdgets') }}\n"
        "{% if snorbleflix %}fine{% endif %} {# a commnet #}"
    )
    assert _flagged(body) == []


def test_urls_emails_code_caps_and_digits_are_skipped() -> None:
    body = (
        "See https://exmaple.test/zorp?qux=blarg or www.flimflux.test and http:/brokn\n"
        "Write to zelda.qwump@exmaple.test or @qwumpster; `kubectl --flagz` runs\n"
        "on blorptown.test, e.g. this. NASAQ ZZYZX 3rd 2fa h4x0r snake_cse"
    )
    assert _flagged(body) == []


def test_contractions_possessives_and_hyphenated_words() -> None:
    assert _flagged("I don't think you\u2019re wrong; the team's well-known co-worker") == []
    assert _flagged("a well-knwon co-wroker") == ["knwon", "wroker"]


def test_a_linkedin_subject_is_never_checked() -> None:
    assert spelling.spelling_issues(LINKEDIN, "Qestion", "Hi {{ first_name }}", frozenset()) == []


def test_a_template_that_does_not_lex_gets_no_spelling_warnings() -> None:
    assert _flagged("recieve {{ 'unterminated }}") == []


def test_the_dictionary_is_checked_ignoring_case() -> None:
    assert _flagged("Zorblatt and Quuxwell say hi", known=frozenset({"zorblatt", "quuxwell"})) == []


def test_findings_past_the_cap_are_counted_in_one_more_warning() -> None:
    words = [f"zq{chr(97 + a)}{chr(97 + b)}x" for a in range(8) for b in range(8)]  # 64 words
    issues = _check(" ".join(words))
    assert len(issues) == spelling.MAX_FINDINGS_PER_PART + 1
    last = issues[-1]
    assert (last.field, last.line) == (None, None)
    assert last.message.startswith("14 more words may be misspelled")


def test_an_8000_character_template_is_checked_in_under_50_ms() -> None:
    """The editor lints as you type. Warmed up first (the word list loads once per
    process), and the best of several runs is timed, so a loaded machine's stray pause
    does not fail it; the bound itself is the issue's 50 ms."""
    line = (
        "Hi {{ first_name }}, it has been a while since we worked together at "
        "{{ company }}. I would love to hear what you are building now, and recieve "
        "your thoughts on https://example.test/notes before our next call.\n"
    )
    body = (line * (8_000 // len(line) + 1))[:8_000]
    assert len(body) == 8_000
    _check(body)  # warm up
    best = float("inf")
    for _ in range(5):
        watch = Stopwatch()
        issues = _check(body)
        best = min(best, watch.elapsed)
    assert [i.field for i in issues] == ["recieve"]
    assert best < scaled(0.05), f"best of 5 runs took {best * 1000:.1f} ms"


@pytest.mark.parametrize(
    "body",
    ["a" * 20_000, "a-" * 10_000, "x." * 10_000, "@" * 20_000, "`" * 20_000, "http" * 5_000],
    ids=["letters", "hyphens", "dots", "ats", "backticks", "schemes"],
)
def test_a_long_text_with_no_spaces_is_checked_quickly(body: str) -> None:
    """#535 review: no skip pattern scans past its chunk, and an overlong word is not
    checked, so 20,000 characters with no space stay fast. Warmed up and best of three,
    like the 8,000-character test; the bound is generous but far under the seconds the
    quadratic patterns took."""
    _check("warm up")
    best = float("inf")
    for _ in range(3):
        watch = Stopwatch()
        _check(body)
        best = min(best, watch.elapsed)
    assert best < scaled(0.1), f"best of 3 runs took {best * 1000:.1f} ms"


# --- the user dictionary -------------------------------------------------------------


def test_contact_names_companies_your_name_and_built_ins_are_seeded(session: Session) -> None:
    user = factories.make_user(session, display_name="Pemberly Vantorq")
    other = factories.make_user(session, kind=UserKind.HOSTED)
    factories.make_contact(
        session,
        user,
        first_name="Zelphine",
        last_name="Okonquist",
        preferred_name="Zel",
        current_company="Brindlewick Labs",
    )
    factories.make_contact(session, other, first_name="Theirsworth")
    body = (
        "Hi Zelphine Okonquist of Brindlewick, Pemberly Vantorq here on LinkedIn "
        "InMail, via netkeep. Theirsworth"
    )
    flagged = [
        i.field for i in template_service.spelling_warnings(session, user, EMAIL, None, body)
    ]
    assert flagged == ["Theirsworth"]  # another user's contact is not in your dictionary


def test_added_words_are_listed_cleaned_and_removable(session: Session) -> None:
    user = factories.make_user(session)
    assert user_dictionary.add_words(session, user, ["Gloopa", "gloopa", "Snarf\u2019s"]) == [
        "gloopa",
        "snarf's",
    ]
    assert user_dictionary.add_words(session, user, ["GLOOPA"]) == []
    assert user_dictionary.added_words(session, user) == ["gloopa", "snarf's"]
    body = "Gloopa says hi"
    assert template_service.spelling_warnings(session, user, EMAIL, None, body) == []
    assert user_dictionary.remove_words(session, user, ["Gloopa", "linkedin"]) == ["gloopa"]
    assert user_dictionary.added_words(session, user) == ["snarf's"]
    assert [
        i.field for i in template_service.spelling_warnings(session, user, EMAIL, None, body)
    ] == ["Gloopa"]


@pytest.mark.parametrize("word", ["", "   ", "two words", "abc123", "x" * 65, "snake_case", "a.b"])
def test_the_dictionary_refuses_what_is_not_a_word(session: Session, word: str) -> None:
    user = factories.make_user(session)
    with pytest.raises(user_dictionary.InvalidWord):
        user_dictionary.add_words(session, user, [word])


def test_dictionary_words_and_contact_names_are_never_logged(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = factories.make_user(session)
    factories.make_contact(session, user, first_name="Wexmorrow", last_name="Plinth")
    user_dictionary.add_words(session, user, ["Quorvane"])
    user_dictionary.remove_words(session, user, ["Quorvane"])
    template_service.spelling_warnings(session, user, EMAIL, "Wexmorrow", "Quorvane recieve")
    logged = caplog.text.lower()
    for word in ("wexmorrow", "plinth", "quorvane", "recieve"):
        assert word not in logged


def test_spelling_never_reaches_stored_lint_or_the_activation_gate(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        row = template_service.create_template(
            session,
            user,
            name="t",
            channel=EMAIL,
            subject="Qestion",
            body="Hi {{ first_name }} teh",
        )
        assert row.lint_json == []
        assert template_service.activation_errors(row) == []
        assert template_service.block_reason(row) is None
        warnings = template_service.spelling_warnings(session, user, EMAIL, "Qestion", row.body)
        assert [i.field for i in warnings] == ["Qestion", "teh"]


def _broken_check(*_args: object, **_kwargs: object) -> list[LintIssue]:
    raise RuntimeError("the word Glimmerwick broke it")


def test_spelling_fails_soft_and_logs_only_the_error_type(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = factories.make_user(session)
    monkeypatch.setattr(template_service, "spelling_issues", _broken_check)
    assert template_service.spelling_warnings(session, user, EMAIL, "Hi", "recieve") == []
    assert "RuntimeError" in caplog.text
    assert "Glimmerwick" not in caplog.text


@pytest.mark.parametrize(
    "stored", ['{"not": "a list"}', '[1, null, "gloopa"]', '"gloopa"', "not json at all"]
)
def test_a_malformed_dictionary_row_reads_as_its_valid_words_or_none(
    session_factory: sessionmaker[Session], stored: str
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        set_setting(session, user, user_dictionary.SETTING_KEY, [])
        session.execute(
            text("UPDATE settings_kv SET value = :value WHERE key = :key"),
            {"value": stored, "key": user_dictionary.SETTING_KEY},
        )
        session.expire_all()
        expected = ["gloopa"] if stored.startswith("[") else []
        assert user_dictionary.added_words(session, user) == expected
        warnings = template_service.spelling_warnings(session, user, EMAIL, None, "gloopa recieve")
        assert [i.field for i in warnings] == (["recieve"] if expected else ["gloopa", "recieve"])
        # The row can still be written: adding a word replaces what was unreadable.
        user_dictionary.add_words(session, user, ["Snorfle"])
        assert "snorfle" in user_dictionary.added_words(session, user)


# --- the API -----------------------------------------------------------------------------


def _local_user(app: FastAPI, session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


async def test_the_editor_lint_has_spelling_and_add_to_dictionary_clears_it(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local_user(running_app, session)
        factories.make_contact(session, user, first_name="Ystrella")
    draft = {
        "channel": "email",
        "subject": "Hi",
        "body": "Hi {{ first_name }}, Ystrella and Vexilo",
    }
    response = await client.post("/api/v1/templates/lint", json=draft, headers=CSRF)
    assert response.status_code == 200, response.text
    assert [(i["rule"], i["severity"], i["field"], i["line"]) for i in response.json()] == [
        ("spelling", "warning", "Vexilo", 1)
    ]

    added = await client.post(
        "/api/v1/templates/dictionary", json={"words": ["Vexilo"]}, headers=CSRF
    )
    assert added.status_code == 200, added.text
    assert added.json() == ["vexilo"]
    assert (await client.get("/api/v1/templates/dictionary")).json() == ["vexilo"]
    again = await client.post("/api/v1/templates/lint", json=draft, headers=CSRF)
    assert again.json() == []

    removed = await client.delete("/api/v1/templates/dictionary/Vexilo", headers=CSRF)
    assert removed.status_code == 204
    gone = await client.delete("/api/v1/templates/dictionary/vexilo", headers=CSRF)
    assert gone.status_code == 404
    assert (await client.get("/api/v1/templates/dictionary")).json() == []


async def test_saved_template_lint_has_no_spelling(client: httpx.AsyncClient) -> None:
    body = {
        "name": "t",
        "channel": "email",
        "subject": "Qestion",
        "body": "Hi {{ first_name }} teh",
    }
    response = await client.post("/api/v1/templates", json=body, headers=CSRF)
    assert response.status_code == 201, response.text
    assert response.json()["lint"] == []


@pytest.mark.parametrize("word", ["two words", "Zorbleplox" * 7])
async def test_adding_a_word_that_is_not_one_is_422_without_echoing_it(
    client: httpx.AsyncClient, word: str
) -> None:
    response = await client.post(
        "/api/v1/templates/dictionary", json={"words": [word]}, headers=CSRF
    )
    assert response.status_code == 422
    assert word not in response.text
    assert "zorbleplox" not in response.text.lower()


async def test_editor_lint_keeps_its_errors_when_spelling_fails(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#535 review: spelling fails soft, so the editor still gets lint's real findings."""
    monkeypatch.setattr(template_service, "spelling_issues", _broken_check)
    draft = {"channel": "email", "subject": "Hi", "body": "Hi {{ compnay }} recieve"}
    response = await client.post("/api/v1/templates/lint", json=draft, headers=CSRF)
    assert response.status_code == 200, response.text
    assert [i["rule"] for i in response.json()] == ["undefined_variable", "no_contact_field"]
    assert "RuntimeError" in caplog.text and "Glimmerwick" not in caplog.text


# --- the CLI -------------------------------------------------------------------------------


def test_cli_dictionary_add_list_remove(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEP_DATABASE_URL", url)
    engine = make_engine(url)
    try:
        migrations.upgrade(engine)
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            ensure_local_user(session)
    finally:
        engine.dispose()
    runner = CliRunner()

    result = runner.invoke(cli, ["templates", "dictionary", "list"])
    assert (result.exit_code, result.output) == (0, "no words added\n")

    result = runner.invoke(cli, ["templates", "dictionary", "add", "Zorvex", "plumbix", "zorvex"])
    assert (result.exit_code, result.output) == (0, "added 2 words\n"), result.output
    result = runner.invoke(cli, ["templates", "dictionary", "add", "PLUMBIX"])
    assert result.output == "added 0 words; 1 already there\n"
    result = runner.invoke(cli, ["templates", "dictionary", "list"])
    assert result.output == "plumbix\nzorvex\n"

    result = runner.invoke(cli, ["templates", "dictionary", "remove", "Zorvex", "linkedin"])
    assert (result.exit_code, result.output) == (0, "removed 1 word; 1 not in your added words\n")
    result = runner.invoke(cli, ["templates", "dictionary", "list"])
    assert result.output == "plumbix\n"

    result = runner.invoke(cli, ["templates", "dictionary", "add", "abc123"])
    assert result.exit_code == 1
    assert "letters only" in result.output
