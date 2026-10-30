"""netkeep.linkedin.flight: the RSC flight grammar, read defensively (#187).

Every malformed payload is ``RouteChanged`` -- never ``KeyError``, ``ValueError``, or a
``JSONDecodeError`` -- and every error names a shape, never a value. The payloads here
are written by hand; none came from a capture.
"""

from __future__ import annotations

import contextlib
import logging

import pytest

from netkeep.linkedin import flight
from netkeep.linkedin.flight import element_props, is_element, is_whole, parse_flight
from netkeep.linkedin.voyager import RouteChanged

E = "test/flight"


def test_rows_modules_and_hints_are_told_apart() -> None:
    body = (
        b'1:I["fake-chunk",[],"TriggerButton"]\n'
        b':HL["/fake.css","style"]\n'
        b'0:["$","$L1",null,{"children":"$L2"}]\n'
        b'2:{"a":1}\n'
        b"7:null\n"
        b'15:"$Sreact.suspense"\n'
    )
    payload = parse_flight(body, endpoint=E)
    assert set(payload.rows) == {"0", "2", "7", "15"}
    assert payload.rows["7"] is None and payload.rows["15"] == "$Sreact.suspense"
    assert payload.modules == {"1": ["fake-chunk", [], "TriggerButton"]}
    assert payload.resolve("$L2") == {"a": 1}
    assert payload.resolve("$2") == {"a": 1}
    assert payload.resolve("$L1") is None  # a module, not a model row
    assert payload.resolve("$undefined") is None
    assert payload.resolve("$Sreact.suspense") is None


def test_a_text_row_is_read_by_its_byte_length_even_across_newlines() -> None:
    text = "line one\nline two é"  # 21 bytes in UTF-8
    raw = text.encode("utf-8")
    body = b"a:T" + format(len(raw), "x").encode() + b"," + raw + b'0:{"x":"$a"}\n'
    payload = parse_flight(body, endpoint=E)
    assert payload.texts == {"a": text}
    assert payload.rows == {"0": {"x": "$a"}}


def test_a_str_body_is_accepted_like_bytes() -> None:
    assert parse_flight('0:{"x":1}\n', endpoint=E).rows == {"0": {"x": 1}}


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"", "empty flight payload"),
        (b"   \n", "empty flight payload"),
        (b'0{"x":1}\n', "no ':' after the row id"),
        (b"0:{not json}\n", "JSON that does not parse"),
        (b"0:<html>\n", "a model row that is not JSON"),
        (b'0:{"x":1}\n0:{"y":2}\n', "a row id appears twice"),
        (b'0:{"x":1}\n2:E{"message":"boom"}\n', "the server sent an error row"),
        (b"a:T40,short", "a text row runs past the payload"),
        (b'1:I["fake",[],"X"]\n', "a flight payload with no model rows"),
        (b"<!doctype html><html></html>", "no ':' after the row id"),
    ],
)
def test_a_malformed_payload_is_route_changed(body: bytes, detail: str) -> None:
    with pytest.raises(RouteChanged) as caught:
        parse_flight(body, endpoint=E)
    assert detail in caught.value.detail
    assert caught.value.endpoint == E


def test_a_walk_follows_references_once_and_survives_a_cycle() -> None:
    payload = parse_flight(b'0:["$L1","$L1"]\n1:{"next":"$L0","leaf":"x"}\n', endpoint=E)
    nodes = list(payload.walk(payload.rows["0"], follow=True, endpoint=E))
    assert nodes.count("x") == 1  # row 1 entered once though referenced twice
    unfollowed = list(payload.walk(payload.rows["0"], follow=False, endpoint=E))
    assert "x" not in unfollowed


def test_a_pathological_payload_is_refused_not_walked_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(flight, "MAX_NODES", 50)
    payload = parse_flight(b"0:" + str(list(range(100))).encode() + b"\n", endpoint=E)
    with pytest.raises(RouteChanged, match="a walk passed 50 nodes"):
        list(payload.nodes(endpoint=E))


def test_elements_are_four_item_lists_with_dict_props() -> None:
    assert is_element(["$", "div", None, {"a": 1}])
    assert element_props(["$", "$L3", "k", {"componentKey": "x"}]) == {"componentKey": "x"}
    assert not is_element(["$", "div", None, "props"])
    assert not is_element(["x", "div", None, {}])
    assert not is_element(["$", "div", {}])
    assert element_props({"$": 1}) is None


def test_the_max_nodes_bound_is_pinned() -> None:
    assert flight.MAX_NODES == 2_000_000


# --- whole or cut short (#203, #207 review) ------------------------------------------------


def _whole(body: bytes) -> bool:
    return is_whole(parse_flight(body, endpoint="t"), endpoint="t")


#: A small answer written child first, as React's ``outlineModel`` orders rows and the
#: fixtures write them: every row the root reaches arrives before the root.
CHILD_FIRST = (
    b'1:I["chunk",[],"TriggerButton"]\n'
    b"2:T5,hello\n"
    b'a:["$","span",null,{"children":["$2"]}]\n'
    b'b:{"then":"$@c"}\n'
    b'c:["$","p",null,{"children":["$La"]}]\n'
    b'3:["$","div",null,{"children":["$Lc","$b:then"]}]\n'
    b'0:["$","$L1",null,{"children":"$L3","x":"$undefined","y":"$Sreact.suspense","z":"$-0"}]\n'
)


def test_a_whole_answer_is_whole() -> None:
    assert _whole(CHILD_FIRST)


def test_a_root_first_answer_is_whole_too() -> None:
    lines = CHILD_FIRST.splitlines(keepends=True)
    assert _whole(lines[-1] + b"".join(lines[:-1]))


def _prefixes(body: bytes) -> list[bytes]:
    lines = body.splitlines(keepends=True)
    return [b"".join(lines[:cut]) for cut in range(1, len(lines))]


@pytest.mark.parametrize("root_first", [False, True])
def test_every_cut_at_a_row_boundary_is_not_whole(root_first: bool) -> None:
    """Child first, a cut has no root; root first, a cut names a row it never got."""
    lines = CHILD_FIRST.splitlines(keepends=True)
    body = lines[-1] + b"".join(lines[:-1]) if root_first else CHILD_FIRST
    judged = []
    for prefix in _prefixes(body):
        try:
            judged.append(_whole(prefix))
        except RouteChanged:
            judged.append(False)  # no model row at all
    assert judged and not any(judged)


def test_no_root_is_not_whole() -> None:
    assert not _whole(b'1:["$","div",null,{}]\n')


@pytest.mark.parametrize(
    "marker",
    [
        "$L9",
        "$9",
        "$@9",
        "$Q9",
        "$W9",
        "$K9",
        "$B9",
        "$F9",
        "$R9",
        "$r9",
        "$X9",
        "$x9",
        "$h9",
        "$9:props:children",
    ],
)
def test_every_id_bearing_marker_to_a_missing_row_is_not_whole(marker: str) -> None:
    assert not _whole(b'0:["$","div",null,{"children":"' + marker.encode() + b'"}]\n')
    assert _whole(b'9:{"x":1}\n0:["$","div",null,{"children":"' + marker.encode() + b'"}]\n')


@pytest.mark.parametrize("start", [b"R", b"X"])
def test_a_stream_cut_after_its_first_chunk_is_not_whole(start: bytes) -> None:
    """#196 item 10: an ``$R`` stream (or ``$X`` async iterable) cut after its first
    chunk still has its root, every row the root reaches, and no orphan. Only the
    missing ``C`` row says it was cut."""
    root = b'0:["$","div",null,{"children":"$' + start + b'9"}]\n'
    started = b"9:" + start + b"\n"
    chunk = b'9:{"chunk":1}\n'
    assert not _whole(started + chunk + root)
    assert _whole(started + chunk + b"9:C\n" + root)
    assert _whole(started + chunk + b'9:C{"last":true}\n' + root)


@pytest.mark.parametrize("start", [b"R", b"r", b"X", b"x"])
def test_a_stream_of_several_chunks_is_read_whole_in_order(start: bytes) -> None:
    """#698: every chunk of a stream arrives as a row of the stream's id. Two or more
    are the stream's chunks, kept in order, never "a row id appears twice"; a
    lower-case ``r`` or ``x`` start is a stream like ``R`` and ``X``."""
    root = b'0:["$","div",null,{"children":"$' + start.upper() + b'9"}]\n'
    body = b"9:" + start + b'\n9:{"chunk":1}\n9:{"chunk":2}\n9:["$L7"]\n9:C\n'
    body += b'7:{"leaf":"q"}\n' + root
    payload = parse_flight(body, endpoint=E)
    assert payload.streams == {"9"} and payload.closed == {"9"}
    assert payload.chunks == {"9": ({"chunk": 1}, {"chunk": 2}, ["$L7"])}
    assert set(payload.rows) == {"0", "7"}
    assert "q" in list(payload.nodes(endpoint=E))
    assert _whole(body)
    # A row only a chunk reaches is reached; without the chunk it is an orphan.
    assert not _whole(body.replace(b'9:["$L7"]\n', b""))
    # Still cut short without its close.
    assert not _whole(body.replace(b"9:C\n", b""))


def test_a_chunk_reference_to_a_missing_row_is_not_whole() -> None:
    root = b'0:["$","div",null,{"children":"$R9"}]\n'
    assert not _whole(b'9:R\n9:{"a":"$L5"}\n9:C\n' + root)


@pytest.mark.parametrize(
    "body",
    [
        b'9:R\n9:R\n9:C\n0:{"x":"$R9"}\n',
        b'9:{"early":1}\n9:R\n9:C\n0:{"x":"$R9"}\n',
    ],
)
def test_a_stream_started_twice_or_after_its_row_is_route_changed(body: bytes) -> None:
    with pytest.raises(RouteChanged) as caught:
        parse_flight(body, endpoint=E)
    assert "a row id appears twice" in caught.value.detail


@pytest.mark.parametrize(
    "body",
    [
        b'0:{"a":1}\n5:R\n5:{"x":"y"}\n5:C\n',
        b'0:{"a":1}\n5:R\n5:{"s":"$9"}\n5:C\n',
    ],
)
def test_a_stream_nothing_reaches_is_not_whole(body: bytes) -> None:
    """#698 review: a stream is held to the reachability rule like a row, so neither an
    unreferenced stream nor a dangling reference inside its chunk passes."""
    assert not _whole(body)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b'9:R\n9:{"a":1}\n9:C\n9:{"late":1}\n0:{"x":"$R9"}\n', "a stream's chunk after its close"),
        (b':R\n0:{"x":1}\n', "a stream start with no row id"),
        (b':r\n0:{"x":1}\n', "a stream start with no row id"),
        (b':x\n0:{"x":1}\n', "a stream start with no row id"),
    ],
)
def test_a_late_chunk_or_an_idless_stream_is_route_changed(body: bytes, detail: str) -> None:
    with pytest.raises(RouteChanged) as caught:
        parse_flight(body, endpoint=E)
    assert detail in caught.value.detail


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b'9:R\n9:{"chunk":1}\n8:C\n', "row 3: a stream's close with no start"),
        (b'9:C\n9:R\n9:{"chunk":1}\n', "row 1: a stream's close with no start"),
        (b'9:R\n9:{"chunk":1}\n9:C\n9:C\n', "row 4: a stream closed twice"),
    ],
    ids=["another-stream", "before-its-start", "twice"],
)
def test_a_close_for_a_stream_not_open_is_route_changed(body: bytes, detail: str) -> None:
    """#719 re-review: a ``C`` row closes a stream this copy started and has not closed.
    One for a stream that never started (or not yet), or a second close, is refused by
    the parse and by the count alike, in fixed words."""
    body += b'0:["$","div",null,{"children":"$R9"}]\n'
    for read in (parse_flight, flight.whole_check):
        with pytest.raises(RouteChanged) as caught:
            read(body, endpoint=E)
        assert caught.value.detail == detail


def test_an_orphan_row_is_not_whole() -> None:
    """A row nothing reaches from the root: a whole answer sends none."""
    assert not _whole(b'5:{"stray":true}\n0:["$","div",null,{}]\n')


def test_the_root_row_and_the_markers_are_pinned() -> None:
    assert flight.ROOT_ROW == "0"
    for text in ("$undefined", "$Sreact.suspense", "$-0", "$NaN", "$Infinity", "$D2026", "$n12"):
        assert flight._ROW_MARKER.fullmatch(text) is None


# --- #530: which rule a copy breaks, as counts only ---------------------------------------

_SECRET = b"Quorbelle Fictionary fake-530-slug"


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        (b'5:{"a":"' + _SECRET + b'"}\n', "root_missing"),
        (b'0:{"x":"$R9"}\n9:R\n9:{"a":"' + _SECRET + b'"}\n', "unclosed_streams"),
        (b'0:{"a":1}\nfff:{"stray":"' + _SECRET + b'"}\n', "unreached_rows"),
        (b'0:{"a":1}\n5:R\n5:{"x":"' + _SECRET + b'"}\n5:C\n', "unreferenced_streams"),
        (b'0:{"a":"$L5","b":"' + _SECRET + b'"}\n', "dangling_references"),
        (
            b'9:R\n9:{"a":1}\n9:C\n9:{"late":"' + _SECRET + b'"}\n0:{"x":"$R9"}\n',
            "late_chunks",
        ),
        (b':R\n:{"a":"' + _SECRET + b'"}\n:C\n0:{"x":1}\n', "empty_id_streams"),
    ],
)
def test_whole_check_names_the_rule_a_copy_breaks(body: bytes, rule: str) -> None:
    """#530: each rule :func:`is_whole` holds a copy to is counted on its own, and the
    count's words carry no row id, value or text of the copy."""
    check = flight.whole_check(body, endpoint=E)
    assert getattr(check, rule) == 1
    counts = {name: getattr(check, name) for name in flight.WholeCheck.__slots__}
    # A copy with no root reaches no row; an idless stream is one nothing can name.
    also = {"root_missing": "unreached_rows", "empty_id_streams": "unreferenced_streams"}
    assert {name for name, count in counts.items() if count} <= {rule, also.get(rule, rule)}
    words = flight.why_not_whole(body, endpoint=E)
    assert f"{rule.replace('_', ' ').replace('empty id', 'empty-id')} 1" in words
    for secret in (*_SECRET.decode().split(), "fff", "$R9", "$L5", "stray"):
        assert secret not in words, secret


def test_whole_check_counts_every_break_not_just_the_first() -> None:
    body = b'0:{"a":"$L5","b":"$L6"}\n7:{"x":1}\n8:{"y":2}\n'
    check = flight.whole_check(body, endpoint=E)
    assert (check.dangling_references, check.unreached_rows) == (2, 2)


def test_whole_check_finds_nothing_in_a_whole_copy() -> None:
    body = b'9:R\n9:{"chunk":"$L7"}\n9:C\n7:{"leaf":1}\n0:["$","div",null,{"c":"$R9"}]\n'
    assert _whole(body)
    assert flight.whole_check(body, endpoint=E) == flight.WholeCheck()


def _corpus() -> list[bytes]:
    """Whole and cut copies, each of :func:`is_whole`'s rules broken and kept, for the
    tie between the two walks below."""
    # Left out on purpose: a late chunk (after its stream's close) and an empty-id
    # stream start. ``parse_flight`` refuses both, so ``is_whole`` gives no verdict to
    # tie the count to; test_whole_check_leaves_parse_flight_strict covers them.
    lines = CHILD_FIRST.splitlines(keepends=True)
    root_first = lines[-1] + b"".join(lines[:-1])
    root = b'0:["$","div",null,{"children":"$R9"}]\n'
    bodies = [CHILD_FIRST, root_first, *_prefixes(CHILD_FIRST), *_prefixes(root_first)]
    bodies += [b"".join(lines[:cut] + lines[cut + 1 :]) for cut in range(len(lines))]
    bodies += [
        b'1:["$","div",null,{}]\n',
        b'5:{"stray":true}\n0:["$","div",null,{}]\n',
        b'0:{"a":"$L5","b":"$L6"}\n7:{"x":1}\n8:{"y":2}\n',
        b'9:R\n9:{"chunk":"$L7"}\n9:C\n7:{"leaf":1}\n0:["$","div",null,{"c":"$R9"}]\n',
        b'9:R\n9:{"chunk":1}\n' + root,
        b'9:R\n9:{"chunk":1}\n9:C\n' + root,
        b'9:R\n9:{"chunk":1}\n8:C\n' + root,
        b'9:R\n9:{"a":"$L5"}\n9:C\n' + root,
        b'0:{"a":1}\n5:R\n5:{"x":"y"}\n5:C\n',
        b'0:{"a":1}\n5:R\n5:{"s":"$9"}\n5:C\n',
    ]
    for marker in (b"$L9", b"$@9", b"$R9", b"$x9", b"$9:props:children"):
        element = b'0:["$","div",null,{"children":"' + marker + b'"}]\n'
        bodies += [element, b'9:{"x":1}\n' + element]
    return bodies


def test_whole_check_is_clean_exactly_when_is_whole() -> None:
    """#736 review: the count and the verdict are two walks of one rule set, so a copy
    is whole exactly when the count finds nothing, in both directions."""
    verdicts: set[bool] = set()
    for body in _corpus():
        try:
            whole = is_whole(parse_flight(body, endpoint=E), endpoint=E)
        except RouteChanged:
            # No model row at all: the count refuses it the same way.
            with pytest.raises(RouteChanged):
                flight.whole_check(body, endpoint=E)
            continue
        verdicts.add(whole)
        clean = flight.whole_check(body, endpoint=E) == flight.WholeCheck()
        assert clean is whole, body
    assert verdicts == {True, False}  # the corpus holds both kinds


def test_why_not_whole_names_an_unreadable_copy_by_type_only() -> None:
    body = b'0:{"a":' + _SECRET + b"\n"
    assert flight.why_not_whole(body, endpoint=E) == "not counted (RouteChanged)"


@pytest.mark.parametrize(
    ("body", "warnings"),
    [
        (b'0:{"a":' + _SECRET + b"\n", 1),
        # The count reads a late chunk the parse refuses: still one warning, the parse's.
        (b'9:R\n9:{"a":1}\n9:C\n9:{"late":1}\n0:{"x":"$R9"}\n', 1),
        (b'0:{"a":"$L5"}\n', 0),
    ],
    ids=["unparseable", "late-chunk", "parses"],
)
def test_why_not_whole_logs_no_second_route_changed_warning(
    body: bytes, warnings: int, caplog: pytest.LogCaptureFixture
) -> None:
    """#736 review: a caller asks why after its own parse of the copy, which logged the
    one ``route changed:`` warning a copy that does not parse gets. Asking why logs
    none of its own."""
    caplog.set_level(logging.WARNING, logger="netkeep")
    with contextlib.suppress(RouteChanged):
        is_whole(parse_flight(body, endpoint=E), endpoint=E)
    flight.why_not_whole(body, endpoint=E)
    logged = [r for r in caplog.records if r.getMessage().startswith("route changed:")]
    assert len(logged) == warnings


def test_whole_check_leaves_parse_flight_strict() -> None:
    """Counting the two stream rules is the report's alone: :func:`parse_flight`
    still refuses a late chunk and an idless stream start."""
    for body in (b'9:R\n9:C\n9:{"late":1}\n0:{"x":"$R9"}\n', b':R\n0:{"x":1}\n'):
        flight.whole_check(body, endpoint=E)
        with pytest.raises(RouteChanged):
            parse_flight(body, endpoint=E)
