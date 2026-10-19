"""The support bundle's redaction (#603), over invented values only."""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from netkeep.logging_setup import PERSONAL_PATTERN, _mark
from netkeep.redaction import SECRET, Redactor, classify, strip_secrets

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
        "phone +44 (0)20 7946 0958 end",
        "phone 020 7946 0958 end",
        "phone 07946 095800 end",
        "phone 555-0142 end",
    ],
)
def test_the_second_line_of_defense_catches_unmarked_values(line: str) -> None:
    out = Redactor().text(line)
    for fragment in (
        EMAIL,
        "ACoAAQuorbelleX9",
        SLUG,
        "010 4477",
        "010-4477",
        "5550104477",
        "7946",
        "0142",
    ):
        assert fragment not in out


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-17 14:16:12 INFO netkeep.worker run 12 finished in 340.5s",
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
    data = home / "Library" / "Application Support" / "netkeep"
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
        ("token: inventedColonToken", "inventedColonToken"),
        ("csrf-token: ajax:1234567890", "ajax:1234567890"),
        ('JSESSIONID="ajax:8888"', "ajax:8888"),
        ("the value ajax:9876543210 alone", "ajax:9876543210"),
        ("q=refresh_token%3D1inventedEncoded%26x", "1inventedEncoded"),
        ("{'name': 'li_at', 'value': 'AQEDinventedDict', 'path': '/'}", "AQEDinventedDict"),
        ('{"name": "JSESSIONID", "value": "inventedDictJ", "secure": true}', "inventedDictJ"),
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


def test_a_traceback_keeps_its_exception_type_but_not_its_message_() -> None:
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


def test_a_devtools_path_loses_its_session_id() -> None:
    out = Redactor().text("attached ws://127.0.0.1:9222/devtools/browser/0b1d-invented-id")
    assert out == "attached ws://127.0.0.1:9222/devtools/<id>"


def _traceback(*body: str) -> str:
    return "\n".join(
        [
            "2026-10-17 14:16:12 ERROR x it failed",
            "Traceback (most recent call last):",
            '  File "/srv/app.py", line 3, in handler',
            "    do_it(name)",
            "    ~~~~~^^^^^^",
            *body,
            "2026-10-17 14:16:13 INFO x next record",
        ]
    )


@pytest.mark.parametrize(
    ("body", "kept"),
    [
        # SQLAlchemy: the message runs on with the statement and its parameters.
        (
            [
                "sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE failed",
                "[SQL: INSERT INTO contacts (first_name) VALUES (?)]",
                f"[parameters: ('{NAME}',)]",
                "(Background on this error at: https://sqlalche.me/e/20/gkpj)",
            ],
            ["sqlalchemy.exc.IntegrityError: <message left out>", "<message left out>"],
        ),
        # pydantic: the detail lines carry the input.
        (
            [
                "pydantic_core._pydantic_core.ValidationError: 1 validation error for M",
                "name",
                f"  String should be short [type=too_long, input_value='{NAME}', input_type=str]",
            ],
            [
                "pydantic_core._pydantic_core.ValidationError: <message left out>",
                "<message left out>",
            ],
        ),
        # A message over several lines.
        (
            [f"ValueError: first {NAME}", f"{NAME} again", "Quorbelle"],
            ["ValueError: <message left out>", "<message left out>"],
        ),
        # A bare name on its own line looks like a type; it is not one.
        (["KeyError", "Fictionary"], ["KeyError", "<message left out>"]),
    ],
)
def test_a_traceback_keeps_only_its_shape(body: list[str], kept: list[str]) -> None:
    out = Redactor().log(_traceback(*body)).splitlines()
    assert NAME not in "\n".join(out)
    assert "Quorbelle" not in "\n".join(out) and "Fictionary" not in "\n".join(out)
    assert out[:5] == [
        "2026-10-17 14:16:12 ERROR x it failed",
        "Traceback (most recent call last):",
        '  File "/srv/app.py", line 3, in handler',
        "    do_it(name)",
        "    ~~~~~^^^^^^",
    ]
    assert out[5:-1] == kept
    assert out[-1] == "2026-10-17 14:16:13 INFO x next record"


def _real_group() -> str:
    try:
        raise ExceptionGroup(
            "two failed", [ValueError(f"{NAME} bad\n{NAME} second line"), KeyError(SLUG)]
        )
    except ExceptionGroup:
        return traceback.format_exc()


def test_a_real_exception_group_keeps_its_drawing_frames_and_types_only() -> None:
    text = "2026-10-17 14:16:12 ERROR x group\n" + _real_group()
    out = Redactor().log(text)
    assert NAME not in out and SLUG not in out
    lines = out.splitlines()
    assert "  + Exception Group Traceback (most recent call last):" in lines
    assert any(line.startswith('  |   File "') for line in lines)
    assert "  | ExceptionGroup: <message left out>" in lines
    assert "  +-+---------------- 1 ----------------" in lines
    assert "    | ValueError: <message left out>" in lines
    assert "    +---------------- 2 ----------------" in lines
    assert "    | KeyError: <message left out>" in lines
    assert "    +------------------------------------" in lines


@pytest.mark.parametrize(
    "message",
    [
        "AcmeError",
        "QuenchwaterError: hi",
        "jane.doe",
        "evander.gallowglass: hi",
        "Quenchwater.Inc",
        "5550142266",
        'File "Evander Gallowglass", line 1, in Vexshire\n    Lucinda Thornbury',
    ],
)
def test_after_a_type_every_line_is_message(message: str) -> None:
    text = _traceback("ValueError: first", *message.split("\n"))
    out = Redactor().log(text).splitlines()
    assert out[5:-1] == ["ValueError: <message left out>", "<message left out>"]


def test_a_multi_line_message_outside_a_traceback_keeps_its_first_line() -> None:
    text = f"2026-10-17 14:16:12 WARNING x validation failed\n  name\n    input_value='{NAME}'"
    assert Redactor().log(text) == (
        "2026-10-17 14:16:12 WARNING x validation failed\n<message left out>"
    )


def test_a_frame_line_keeps_only_frame_shape() -> None:
    # A frame without a source line: the next line is not taken for code.
    lines = (
        Redactor()
        .log(
            "2026-10-17 14:16:12 ERROR x\nTraceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            f"ValueError: {NAME}"
        )
        .splitlines()
    )
    assert lines[-1] == "ValueError: <message left out>"


@pytest.mark.parametrize(
    "text",
    [
        "range 3600-7200",
        "took 250-1250 ms",
        "commit 789f423-1234",
        "0 1000-2000 1",
        "visits 100-1000",
        "a 100-1000\n2026-10-17 15:06:16 INFO b",
        "budget 555 0100 then",
    ],
)
def test_numbers_that_are_not_phones_stay_and_records_stay_apart(text: str) -> None:
    assert Redactor().text(text) == text
    record = f"2026-10-17 15:06:15 INFO {text}"
    assert Redactor().log(record) == record


@pytest.mark.parametrize(
    "line",
    [
        "phone 555-0142 end",
        "call me at 555-0142",
        "+33 1 23 45 67 89 here",
        "01 23 45 67 89 here",
        "+44 (0)20 7946 0958",
    ],
)
def test_phone_formats_are_caught(line: str) -> None:
    out = Redactor().text(line)
    assert "<phone-1>" in out
    for fragment in ("0142", "45 67 89", "7946"):
        assert fragment not in out


def test_a_cookie_dict_with_the_value_first() -> None:
    out = strip_secrets("{'value': 'AQEDinventedFirst', 'name': 'li_at', 'path': '/'}")
    assert "AQEDinventedFirst" not in out
    assert "'value': '<secret>'" in out
