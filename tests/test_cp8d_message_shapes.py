"""Shapes from the CP8d captures (#520, #558): an unnamed group, and the realtime event.

Both are invented, built from key names only ("Quorbelle Fictionary" style values). The
capture's group list item was never seen: its shape comes from a single-conversation answer
of the same type. The realtime event is never read by netkeeper; the test pins that its
``result`` is the ``Message`` shape ``_message`` already parses, so a future reader can reuse it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import messaging_pages as mp

from netkeeper.linkedin import messaging_shapes as shapes
from netkeeper.linkedin.messaging_shapes import Kind

#: The captured group had never been named: ``title`` null, and no ``conversationType`` key.
UNTITLED_GROUP = dataclasses.replace(mp.GROUP, title=None)


def _item() -> dict[str, Any]:
    return mp.conversation(UNTITLED_GROUP)


def test_an_unnamed_group_has_a_null_title_and_no_conversation_type() -> None:
    item = _item()
    assert item["groupChat"] is True and item["title"] is None
    assert len(item["conversationParticipants"]) == 3
    assert "conversationType" not in item
    assert item["categories"] == [mp.INBOX, mp.PRIMARY_INBOX] and item["state"] is None


def test_an_unnamed_group_is_still_skipped_as_a_group() -> None:
    """Rule 1 reads ``groupChat`` and the participant count, never ``title``."""
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([UNTITLED_GROUP])).items
    assert item.kind is Kind.GROUP
    # The titled fixture lands on the same kind, so a title is not what marks a group.
    [titled] = shapes.parse_conversation_list(mp.conversations_by_sync_token([mp.GROUP])).items
    assert titled.kind is Kind.GROUP


def test_a_group_marked_by_participants_alone_is_still_a_group() -> None:
    """Even with ``groupChat`` false, a third participant makes it a group (rule 1)."""
    odd = dataclasses.replace(UNTITLED_GROUP, group_chat=False)
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([odd])).items
    assert item.kind is Kind.GROUP


def test_the_by_id_answer_of_a_group_is_not_a_list_variant() -> None:
    """``messengerConversationsById`` (a bare object) is ignored, not parsed as a list."""
    body = json.dumps({"data": {"messengerConversationsById": _item()}})
    assert shapes.answer_field(body) is None


def _realtime_result(sent: mp.Msg) -> dict[str, Any]:
    """The captured extras: the full conversation inline, and ``previousMessages`` stubs."""
    result = mp.message(sent)
    result["conversation"] = mp.conversation(
        mp.Conv(sent.conversation, (mp.QUILLON,), sent, creator=mp.QUILLON)
    )
    result["previousMessages"] = {
        "_type": "com.linkedin.restli.common.CollectionResponse",
        "elements": [
            {"_type": "com.linkedin.messenger.Message", "entityUrn": "urn:li:msg_message:stub"}
        ],
    }
    return result


def test_a_realtime_new_message_result_parses_with_message() -> None:
    sent = mp.Msg(16, 5, mp.QUILLON, "Invented realtime line.", mp.INBOUND_LAST.at_ms)
    event: dict[str, Any] = {
        "com.linkedin.realtimefrontend.DecoratedEvent": {
            "topic": "urn:li-realtime:messagesTopic:urn:li-realtime:myself",
            "payload": {
                "$metadata": {},
                "data": {
                    "doDecorateMessageMessengerRealtimeDecoration": {
                        "_type": "com.linkedin.restli.common.ActionResponse",
                        "result": _realtime_result(sent),
                    }
                },
            },
        }
    }
    [body] = event.values()
    decoration = body["payload"]["data"]["doDecorateMessageMessengerRealtimeDecoration"]
    parsed = shapes._message(decoration["result"], owner=mp.OWNER.urn, where="a realtime event")
    assert not parsed.outbound and parsed.sender_urn == mp.QUILLON.urn
    assert parsed.text_snippet == "Invented realtime line."
    # The inline conversation is a full one, and parses as a list item would.
    result = decoration["result"]
    [item] = shapes.parse_conversation_list(
        mp.conversations_by_sync_token([mp.Conv(16, (mp.QUILLON,), sent, creator=mp.QUILLON)])
    ).items
    assert result["conversation"]["entityUrn"] == item.conversation_urn
    assert result["backendConversationUrn"] == result["conversation"]["backendUrn"]
