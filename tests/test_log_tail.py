"""The recent-log-lines endpoint (#655): the cap, the filters, the redaction, the owner.

Log files are written under the test's scratch data directory (conftest points
``NETKEEP_DATA`` there), never the real one. The redaction tests write through the
real logging setup, so the markers are the ones the code puts in.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from test_runs_serve import HEADERS

from netkeep.db import session_scope
from netkeep.logging_setup import (
    LOG_DIRNAME,
    LOG_FILENAME,
    personal,
    personal_exc,
    remove_file_handler,
    setup_logging,
)
from netkeep.models import UserKind
from netkeep.paths import data_dir
from netkeep.redaction import Redactor
from netkeep.services import local_api_key, log_tail

URL = "/api/v1/support/logs"
START = datetime(2026, 10, 1, 12, 0, 0)


def _record(when: datetime, level: str, message: str, logger: str = "netkeep.test") -> str:
    return f"{when:%Y-%m-%d %H:%M:%S} {level} {logger} {message}\n"


def _write(*records: str, name: str = LOG_FILENAME) -> Path:
    directory = data_dir() / LOG_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("".join(records))
    return path


def _messages(body: dict[str, object]) -> list[str]:
    return [line["message"] for line in body["lines"]]  # type: ignore[attr-defined]


async def _get(client: httpx.AsyncClient, **params: object) -> dict[str, object]:
    response = await client.get(URL, params=params, headers=HEADERS)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


# --- the cap and the shape ------------------------------------------------------------


def test_the_cap_is_pinned() -> None:
    assert log_tail.MAX_LINES == 1000
    assert log_tail.DEFAULT_LINES == 200


async def test_the_newest_lines_come_back_oldest_first(client: httpx.AsyncClient) -> None:
    _write(*(_record(START + timedelta(seconds=n), "INFO", f"record {n}") for n in range(300)))
    default = await _get(client)
    assert len(default["lines"]) == 200  # type: ignore[arg-type]
    assert _messages(default)[0] == "record 100"
    assert _messages(default)[-1] == "record 299"
    few = await _get(client, lines=3)
    assert _messages(few) == ["record 297", "record 298", "record 299"]
    first = few["lines"][0]  # type: ignore[index]
    assert first["level"] == "INFO"
    assert first["logger"] == "netkeep.test"
    assert first["at"].startswith("2026-10-01T12:04:57")


async def test_a_request_is_capped_at_the_maximum(client: httpx.AsyncClient) -> None:
    _write(*(_record(START + timedelta(seconds=n), "INFO", f"record {n}") for n in range(1200)))
    body = await _get(client, lines=log_tail.MAX_LINES)
    assert len(body["lines"]) == 1000  # type: ignore[arg-type]
    assert _messages(body)[0] == "record 200"
    for bad in (log_tail.MAX_LINES + 1, 0, -5):
        refused = await client.get(URL, params={"lines": bad}, headers=HEADERS)
        assert refused.status_code == 422


def test_the_service_clamps_a_larger_request(tmp_path: Path) -> None:
    (tmp_path / LOG_FILENAME).write_text(
        "".join(_record(START + timedelta(seconds=n), "INFO", f"r{n}") for n in range(1100))
    )
    tail = log_tail.recent_lines(lines=50_000, directory=tmp_path)
    assert len(tail.lines) == log_tail.MAX_LINES


# --- a missing or empty file ----------------------------------------------------------


async def test_a_missing_log_file_answers_an_empty_list_with_a_reason(
    client: httpx.AsyncClient,
) -> None:
    assert not (data_dir() / LOG_DIRNAME / LOG_FILENAME).exists()
    body = await _get(client)
    assert body == {"lines": [], "reason": "no_log_file"}


async def test_an_empty_log_file_answers_an_empty_list_with_a_reason(
    client: httpx.AsyncClient,
) -> None:
    _write()
    assert await _get(client) == {"lines": [], "reason": "empty_log"}


# --- the filters ----------------------------------------------------------------------


async def test_the_level_filter_keeps_that_level_and_above(client: httpx.AsyncClient) -> None:
    _write(
        _record(START, "DEBUG", "d"),
        _record(START + timedelta(seconds=1), "INFO", "i"),
        _record(START + timedelta(seconds=2), "WARNING", "w"),
        _record(START + timedelta(seconds=3), "ERROR", "e"),
        _record(START + timedelta(seconds=4), "CRITICAL", "c"),
    )
    assert _messages(await _get(client, level="debug")) == ["d", "i", "w", "e", "c"]
    assert _messages(await _get(client, level="warning")) == ["w", "e", "c"]
    assert _messages(await _get(client, level="critical")) == ["c"]
    refused = await client.get(URL, params={"level": "loud"}, headers=HEADERS)
    assert refused.status_code == 422


async def test_the_level_filter_applies_before_the_cap(client: httpx.AsyncClient) -> None:
    _write(
        _record(START, "ERROR", "old error"),
        *(_record(START + timedelta(seconds=n), "INFO", f"i{n}") for n in range(1, 20)),
    )
    assert _messages(await _get(client, level="error", lines=5)) == ["old error"]


async def test_the_time_range_filters(client: httpx.AsyncClient) -> None:
    _write(*(_record(START + timedelta(minutes=n), "INFO", f"m{n}") for n in range(10)))
    since = (START + timedelta(minutes=3)).isoformat()
    until = (START + timedelta(minutes=6)).isoformat()
    assert _messages(await _get(client, since=since)) == [f"m{n}" for n in range(3, 10)]
    assert _messages(await _get(client, until=until)) == [f"m{n}" for n in range(7)]
    assert _messages(await _get(client, since=since, until=until)) == [f"m{n}" for n in range(3, 7)]


async def test_a_time_with_an_offset_is_read_as_that_instant(client: httpx.AsyncClient) -> None:
    _write(*(_record(START + timedelta(minutes=n), "INFO", f"m{n}") for n in range(4)))
    instant = (START + timedelta(minutes=2)).astimezone()
    assert _messages(await _get(client, since=instant.isoformat())) == ["m2", "m3"]


async def test_a_filter_that_matches_nothing_says_so(client: httpx.AsyncClient) -> None:
    _write(_record(START, "INFO", "only"))
    assert await _get(client, level="error") == {"lines": [], "reason": "no_match"}


# --- records and rotation -------------------------------------------------------------


async def test_it_reads_across_a_rotated_file_boundary(client: httpx.AsyncClient) -> None:
    _write(
        *(_record(START + timedelta(seconds=n), "INFO", f"old {n}") for n in range(3)),
        name=f"{LOG_FILENAME}.1",
    )
    _write(*(_record(START + timedelta(seconds=n), "INFO", f"new {n}") for n in range(10, 13)))
    assert _messages(await _get(client)) == [
        "old 0",
        "old 1",
        "old 2",
        "new 10",
        "new 11",
        "new 12",
    ]
    assert _messages(await _get(client, lines=4)) == ["old 2", "new 10", "new 11", "new 12"]


async def test_a_traceback_stays_one_record_with_its_shape(client: httpx.AsyncClient) -> None:
    _write(
        _record(START, "ERROR", "boom")
        + "Traceback (most recent call last):\n"
        + '  File "x.py", line 3, in run\n'
        + "ValueError: Quorbelle Fictionary was here\n",
        _record(START + timedelta(seconds=1), "INFO", "after"),
    )
    body = await _get(client)
    assert len(body["lines"]) == 2  # type: ignore[arg-type]
    first = _messages(body)[0]
    assert first.startswith("boom\nTraceback (most recent call last):")
    assert "Quorbelle" not in first


# --- redaction ------------------------------------------------------------------------

NAME = "Quorbelle Fictionary"
EMAIL = "quorbelle.fictionary@example.test"
SLUG = "quorbelle-fictionary-4x7"
URN = "urn:li:fsd_profile:ACoAAQuorbelleX9"
PHONE = "+1 555 010 4477"
EXC_TEXT = "no such contact Zephyrine Imaginaria"
PERSONAL_VALUES = (
    "Quorbelle", "Fictionary", EMAIL, SLUG, "ACoAAQuorbelleX9", "010 4477",
    "Zephyrine", "Imaginaria", "quorbelleuser",
)  # fmt: skip
SECRET_VALUES = (
    "AQEDinventedLiAtCookie",
    "ajax:8765432109",
    "ya29.a0AinventedAccessTok3n",
    "inventedColonToken",
    "0inventedAuthCode",
    "sk-ant-api03-inventedKey",
    "inventedBearerValue",
    "inventedSessionCookieValue",
)


@pytest.fixture
def written_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Records through the real logging setup: marked values, unmarked ones and secrets."""
    user_dir = tmp_path / "Users" / "quorbelleuser"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_dir))
    setup_logging()
    try:
        log = logging.getLogger("netkeep.test_log_tail")
        log.warning("saved contact %s %s", personal(NAME), personal(EMAIL))
        log.warning(
            "visiting %s (%s)", personal(f"https://www.linkedin.com/in/{SLUG}/"), personal(URN)
        )
        log.warning("phone %s", personal(PHONE))
        log.warning("opened %s", personal(user_dir / "Library" / "x.csv"))
        log.warning("reply from %s at linkedin.com/in/%s", EMAIL, SLUG)
        log.error("Cookie: li_at=AQEDinventedLiAtCookie; JSESSIONID=ajax:8765432109")
        log.error("token ya29.a0AinventedAccessTok3n and sk-ant-api03-inventedKey")
        log.error("csrf-token: ajax:8765432109 and token: inventedColonToken")
        log.error("GET /api/v1/session/connect?state=s&code=0inventedAuthCode")
        log.error("Authorization: Bearer inventedBearerValue")
        try:
            raise ValueError(EXC_TEXT)
        except ValueError as exc:
            log.exception("failed for %s", personal_exc(exc))
        yield
    finally:
        remove_file_handler()


def _leaks(text: str, needles: tuple[str, ...]) -> list[str]:
    return [needle for needle in needles if needle in text]


async def test_marked_personal_values_and_unmarked_ones_never_reach_the_page(
    client: httpx.AsyncClient, written_log: None
) -> None:
    body = await _get(client)
    text = "\n".join(_messages(body))
    assert len(body["lines"]) > 10  # type: ignore[arg-type]
    assert _leaks(text, PERSONAL_VALUES) == []
    assert "<email-" in text  # the scan is not vacuous: placeholders took their place


async def test_secrets_never_reach_the_page(client: httpx.AsyncClient, written_log: None) -> None:
    body = await _get(client)
    text = "\n".join(_messages(body))
    assert _leaks(text, SECRET_VALUES[:6]) == []
    assert "<secret>" in text


async def test_the_local_api_key_cookie_value_and_connect_code_are_removed(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    key = running_app.state.local_api_key
    code = local_api_key.mint_code(key.key)
    bare = [key.key, key.session_value(), code]
    _write(
        _record(START, "ERROR", f"key was {bare[0]} oops"),
        _record(START + timedelta(seconds=1), "ERROR", f"cookie value {bare[1]} oops"),
        _record(START + timedelta(seconds=2), "ERROR", f"code {bare[2]} oops"),
        _record(START + timedelta(seconds=3), "ERROR", f"link /api/v1/session/connect?code={code}"),
    )
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200
    assert _leaks(response.text, tuple(bare)) == []
    assert _leaks(response.text, (code.split(".")[1],)) == []
    assert response.text.count("<secret>") >= 4


async def test_a_session_cookie_value_in_a_header_line_is_removed(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    cookie = running_app.state.local_api_key.session_value()
    _write(_record(START, "INFO", f"Cookie: netkeep_session_8000={cookie}"))
    response = await client.get(URL, headers=HEADERS)
    assert cookie not in response.text


async def test_a_mutation_that_skips_redaction_is_caught(
    client: httpx.AsyncClient, written_log: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With redaction switched off the same scan finds the values, so the tests above
    fail for a change that skips it and are not passing on an empty log."""
    monkeypatch.setattr(Redactor, "log", lambda self, text: text)
    body = await _get(client)
    text = "\n".join(_messages(body))
    assert _leaks(text, PERSONAL_VALUES) != []
    assert _leaks(text, SECRET_VALUES[:6]) != []


# --- who may read ---------------------------------------------------------------------


async def test_only_the_owner_reads_the_logs(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _write(_record(START, "INFO", "visible to the owner"))
    with session_scope(running_app.state.session_factory, write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED).id
    with acting_as(running_app, other):
        response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 403
    assert "visible" not in response.text
    assert _messages(await _get(client)) == ["visible to the owner"]


# --- a personal span that holds a line shaped like a record ---------------------------

SPAN_SECRET = ("Jane", "Doe-Secret", "jane.private")
FORGED = (
    "2026-10-18 09:00:01 Jane Doe-Secret jane.private\u27e7 done\n",  # unknown level
    "2026-10-18 09:00:01 ERROR netkeep.forged Jane Doe-Secret jane.private\u27e7 done\n",
)
OPENER = "2026-10-18 09:00:00 INFO netkeep.x saved \u27e6line one\n"


@pytest.mark.parametrize("forged", FORGED)
async def test_a_span_holding_a_record_shaped_line_is_one_redacted_record(
    client: httpx.AsyncClient, forged: str
) -> None:
    _write(OPENER + forged, _record(START, "INFO", "after"))
    body = await _get(client)
    assert len(body["lines"]) == 2  # type: ignore[arg-type]
    assert _leaks(str(body), SPAN_SECRET) == []
    assert "saved <personal-1> done" in _messages(body)[0]
    assert (await _get(client, level="error"))["reason"] == "no_match"


@pytest.mark.parametrize("forged", FORGED)
async def test_a_span_crossing_a_rotated_file_boundary_is_redacted(
    client: httpx.AsyncClient, forged: str
) -> None:
    _write(OPENER, name=f"{LOG_FILENAME}.1")
    _write(forged, _record(START, "INFO", "after"))
    body = await _get(client)
    assert len(body["lines"]) == 2  # type: ignore[arg-type]
    assert _leaks(str(body), SPAN_SECRET) == []
    assert (await _get(client, level="error"))["reason"] == "no_match"


# --- an unterminated span is capped -----------------------------------------------------


def test_the_span_limits_are_pinned() -> None:
    assert log_tail.SPAN_MAX_LINES == 200
    assert log_tail.SPAN_MAX_BYTES == 64 * 1024


def _unterminated(count: int, *, forged: bool = False) -> list[str]:
    """A cut-short span, then ``count`` ordinary records."""
    cut = "2026-10-18 09:00:00 INFO netkeep.x cut \u27e6Jane Doe-Secret\n"
    more = []
    for n in range(count):
        who = "ERROR netkeep.forged Jane Doe-Secret" if forged and n == 5 else "INFO netkeep.y"
        more.append(f"2026-10-18 09:01:{n % 60:02d} {who} swallowed {n}\n")
    return [cut + "".join(more)]


async def test_an_unterminated_span_ends_its_record_and_drops_the_rest(
    client: httpx.AsyncClient,
) -> None:
    _write(
        *_unterminated(500, forged=True), "ignored\u27e7 tail\n", _record(START, "INFO", "later")
    )
    body = await _get(client)
    assert _messages(body)[0].startswith("cut ")
    assert "<message left out>" in _messages(body)[0]
    assert _messages(body)[-1] == "later"
    assert len(body["lines"]) == 2  # type: ignore[arg-type]
    assert not any("swallowed" in m for m in _messages(body))
    assert _leaks(str(body), SPAN_SECRET) == []
    assert (await _get(client, level="error"))["reason"] == "no_match"


async def test_a_span_just_under_the_line_cap_is_kept_whole(client: httpx.AsyncClient) -> None:
    middle = "".join(f"line {n}\n" for n in range(log_tail.SPAN_MAX_LINES - 3))
    _write(
        "2026-10-18 09:00:00 INFO netkeep.x a \u27e6b\n" + middle + "c\u27e7 end\n",
        _record(START, "INFO", "later"),
    )
    body = await _get(client)
    assert _messages(body)[0].endswith("end")
    assert _messages(body)[-1] == "later"


async def test_a_span_past_the_byte_cap_is_cut(client: httpx.AsyncClient) -> None:
    big = "x" * 1000 + "\n"
    _write(
        "2026-10-18 09:00:00 INFO netkeep.x a \u27e6b\n" + big * 70 + "c\u27e7 end\n",
        _record(START, "INFO", "later"),
    )
    body = await _get(client)
    assert "<message left out>" in _messages(body)[0]
    assert _messages(body)[-1] == "later"


async def test_a_normal_record_after_an_unterminated_marker_still_comes_back(
    client: httpx.AsyncClient,
) -> None:
    _write(*_unterminated(400), "tail\u27e7\n", _record(START, "WARNING", "still here"))
    body = await _get(client, level="warning")
    assert _messages(body) == ["still here"]


def test_a_huge_unterminated_log_stays_small(tmp_path: Path) -> None:
    import tracemalloc

    line = "2026-10-18 09:01:00 INFO netkeep.y " + "a" * 80 + "\n"
    (tmp_path / LOG_FILENAME).write_text(
        "2026-10-18 09:00:00 INFO netkeep.x cut \u27e6Jane\n" + line * (10_000_000 // len(line))
    )
    tracemalloc.start()
    tail = log_tail.recent_lines(directory=tmp_path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2_000_000
    assert len(tail.lines) == 1
    assert len(tail.lines[0].message) < 200


async def test_a_dated_line_with_no_known_level_or_logger_is_not_a_record(
    client: httpx.AsyncClient,
) -> None:
    _write(
        _record(START, "INFO", "first"),
        "2026-10-18 09:00:01 Jane Doe-Secret says hello\n",
        "2026-10-18 09:00:02 NOTICE not.a.level text\n",
    )
    body = await _get(client)
    assert len(body["lines"]) == 1  # type: ignore[arg-type]
    for line in body["lines"]:  # type: ignore[attr-defined]
        assert line["level"] == "INFO"
        assert line["logger"] == "netkeep.test"
