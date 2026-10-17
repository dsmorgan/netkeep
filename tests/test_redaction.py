"""The support bundle's redaction (#603), over invented values only."""

from __future__ import annotations

from pathlib import Path

import pytest

from netkeeper.logging_setup import PERSONAL_PATTERN, _mark
from netkeeper.redaction import SECRET, Redactor, classify, strip_secrets

NAME = "Quorbelle Fictionary"
EMAIL = "quorbelle.fictionary@example.test"
URN = "urn:li:fsd_profile:ACoAAQuorbelleX9"
SLUG = "quorbelle-fictionary-4x7"
PROFILE = f"https://www.linkedin.com/in/{SLUG}/"


def test_a_marked_value_becomes_a_typed_placeholder() -> None:
    redactor = Redactor()
    line = f"saved {_mark(NAME)} with {_mark(EMAIL)} and {_mark(URN)} at {_mark(PROFILE)}"
    out = redactor.text(line)
    assert out == "saved <personal-1> with <email-1> and <urn-1> at <url-1>"


def test_the_same_value_keeps_its_placeholder_across_calls() -> None:
    redactor = Redactor()
    first = redactor.text(f"a {_mark(NAME)}")
    second = redactor.text(f"b {_mark(NAME)} c {_mark('Zephyrine Imaginary')}")
    assert first == "a <personal-1>"
    assert second == "b <personal-1> c <personal-2>"
    assert redactor.counts() == {"personal": 2}


def test_a_span_cut_short_redacts_to_the_end_of_the_line() -> None:
    # `%.20s` drops the closing marker: the pattern takes the rest of the line.
    cut = _mark(f"{NAME} and more text")[:15]
    out = Redactor().text(f"x {cut}\nnext line stays")
    assert NAME.split()[0] not in out
    assert out.endswith("\nnext line stays")


def test_a_marked_span_over_two_lines_is_one_value() -> None:
    out = Redactor().text(f"x {_mark(NAME + chr(10) + 'Second Line')} y")
    assert out == "x <personal-1> y"


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (EMAIL, "email"),
        (URN, "urn"),
        (PROFILE, "url"),
        ("linkedin.com/in/someone", "url"),
        ("+1 555 010 4477", "phone"),
        ("/Users/quorbelle/Library/x.sqlite3", "path"),
        ("'/tmp/x.csv'", "path"),
        (NAME, "personal"),
        ("Zephyr Outreach Q3", "personal"),
    ],
)
def test_classify(value: str, kind: str) -> None:
    assert classify(value) == kind


@pytest.mark.parametrize(
    "line",
    [
        f"unmarked {EMAIL} here",
        f"urn {URN}, then",
        f"profile {PROFILE} done",
        f"bare www.linkedin.com/in/{SLUG}",
        f"path fragment /in/{SLUG}?x=1",
        "phone +1 555 010 4477 end",
        "phone (555) 010-4477 end",
        "phone 555-010-4477 end",
        "phone +15550104477 end",
        "phone +44 20 7946 0958 end",
    ],
)
def test_the_second_line_of_defense_catches_unmarked_values(line: str) -> None:
    out = Redactor().text(line)
    for fragment in (EMAIL, "ACoAAQuorbelleX9", SLUG, "010 4477", "010-4477", "5550104477", "7946"):
        assert fragment not in out


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-17 14:16:12 INFO netkeeper.worker run 12 finished in 340.5s",
        "budget 0/150 today (hard max 400/day), 2026-10-10T18:16:14.673840+00:00",
        "attach over CDP to http://127.0.0.1:9222",
        "migration 0045, version 0.0.1, 192.168.1.10",
        "GET /api/v1/linkedin/runs/42 200",
    ],
)
def test_ordinary_lines_are_left_alone(line: str) -> None:
    assert Redactor().text(line) == line


def test_a_loopback_url_keeps_its_place_but_not_its_query() -> None:
    out = Redactor().text("GET http://127.0.0.1:8000/api/v1/contacts?q=Quorbelle+Fictionary")
    assert out == "GET http://127.0.0.1:8000/api/v1/contacts?<url-1>"


def test_data_and_home_directories_are_replaced(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "quorbelleuser"
    data = home / "Library" / "Application Support" / "netkeeper"
    data.mkdir(parents=True)
    redactor = Redactor([(data, "<data>"), (home, "~")])
    out = redactor.text(f"opened {data}/netkeeper.sqlite3 and {home}/Downloads/a.zip")
    assert out == "opened <data>/netkeeper.sqlite3 and ~/Downloads/a.zip"
    assert "quorbelleuser" not in redactor.text("from /Users/quorbelleuser/elsewhere/x")
    assert "quorbelleuser" not in redactor.text("from /home/quorbelleuser/x")


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("token ya29.a0AinventedAccessTok3n_x-y", "a0AinventedAccessTok3n"),
        ("refresh 1//0inventedRefreshTokenValue", "0inventedRefreshTokenValue"),
        ("client GOCSPX-inventedClientSecret9", "inventedClientSecret9"),
        ("key sk-ant-api03-inventedKeyValue", "inventedKeyValue"),
        ("Authorization: Bearer invented.jwt.value", "invented.jwt.value"),
        ("header bearer inventedBearerValue", "inventedBearerValue"),
        ("Cookie: li_at=AQEDinventedCookie; JSESSIONID=ajax:123", "AQEDinventedCookie"),
        ("cookie li_at=AQEDinventedCookie2 seen", "AQEDinventedCookie2"),
        ("JSESSIONID=ajax:9876543210 set", "ajax:9876543210"),
        ("GET /callback?state=x&code=4/0inventedAuthCode", "0inventedAuthCode"),
        ('{"client_secret": "invented-client-secret-value"}', "invented-client-secret-value"),
        ("{'refresh_token': 'invented-refresh-in-repr'}", "invented-refresh-in-repr"),
        ("csrf_token=inventedCsrfValue&x=1", "inventedCsrfValue"),
        ("password = inventedPassw0rd", "inventedPassw0rd"),
    ],
)
def test_secrets_are_stripped(line: str, secret: str) -> None:
    out = strip_secrets(line)
    assert secret not in out
    assert SECRET in out
    # The redactor strips them too, before anything else.
    assert secret not in Redactor().text(line)


def test_the_pattern_the_redactor_uses_is_the_log_files() -> None:
    # One definition of a marked span: the one #602's file writes.
    assert PERSONAL_PATTERN.fullmatch(_mark(NAME)) is not None


def test_json_values_are_redacted_and_keys_kept() -> None:
    value = {"error": f"no profile at {PROFILE}", "counts": [1, {"note": EMAIL}], "n": 3}
    out = Redactor().value(value)
    assert out == {"error": "no profile at <url-1>", "counts": [1, {"note": "<email-1>"}], "n": 3}


def test_a_traceback_keeps_its_exception_type_but_not_its_message() -> None:
    text = (
        f"2026-10-17 14:16:12 ERROR x failed for {_mark(NAME)}\n"
        "Traceback (most recent call last):\n"
        '  File "/srv/x.py", line 3, in f\n'
        f"ValueError: no such contact {NAME}\n"
    )
    out = Redactor().log(text)
    assert NAME not in out
    assert out.splitlines()[0].endswith("failed for <personal-1>")
    assert out.splitlines()[-1] == "ValueError: <message left out>"


def test_a_checkpoint_path_loses_its_challenge_id() -> None:
    out = Redactor().text("checkpoint at /checkpoint/challenge/AgInventedChallengeId")
    assert out == "checkpoint at /checkpoint/challenge/<slug-1>"
