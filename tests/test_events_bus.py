from datetime import UTC, datetime

import pytest

from netkeep.services.events import Event, EventBus, Subscription


async def _drain(bus: EventBus, subscription: Subscription) -> list[Event]:
    bus.unsubscribe(subscription)
    return [event async for event in subscription]


async def test_publish_reaches_every_subscriber() -> None:
    bus = EventBus()
    one = bus.subscribe()
    two = bus.subscribe()
    assert bus.subscriber_count == 2

    bus.publish(Event(type="demo", data={"n": 1}, user_id=7))

    got = await anext(one)
    assert got.type == "demo"
    assert got.data == {"n": 1}
    assert got.user_id == 7
    assert await anext(two) is got


async def test_unsubscribe_ends_iteration_after_the_backlog() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    bus.publish(Event(type="a"))
    bus.publish(Event(type="b"))

    assert [event.type for event in await _drain(bus, subscription)] == ["a", "b"]
    assert bus.subscriber_count == 0
    with pytest.raises(StopAsyncIteration):
        await anext(subscription)

    bus.publish(Event(type="late"))  # nobody listening: nothing happens
    bus.unsubscribe(subscription)  # idempotent


async def test_overflow_drops_the_oldest_and_counts() -> None:
    bus = EventBus(queue_size=3)
    subscription = bus.subscribe()
    for n in range(5):
        bus.publish(Event(type=f"e{n}"))

    assert [event.type for event in await _drain(bus, subscription)] == ["e2", "e3", "e4"]
    assert subscription.dropped == 2


async def test_a_slow_subscriber_does_not_affect_a_fast_one() -> None:
    bus = EventBus(queue_size=1)
    slow = bus.subscribe()
    fast = bus.subscribe()
    bus.publish(Event(type="one"))
    assert (await anext(fast)).type == "one"
    bus.publish(Event(type="two"))
    assert (await anext(fast)).type == "two"

    assert [event.type for event in await _drain(bus, slow)] == ["two"]
    assert slow.dropped == 1
    assert fast.dropped == 0


def test_event_defaults_and_wire_shape() -> None:
    event = Event(type="x")
    assert event.data == {}
    assert event.user_id is None
    assert event.ts.tzinfo is UTC
    assert event.to_dict() == {
        "type": "x",
        "data": {},
        "ts": event.ts.isoformat(),
        "user_id": None,
    }


def test_event_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Event(type="x", ts=datetime(2026, 1, 1))


def test_latest_keeps_the_last_event_of_each_type_for_each_user() -> None:
    """What the status roll-up reads between saves (#761): one per type and user."""
    bus = EventBus()
    assert bus.latest("run.progress", 7) is None
    first = Event(type="run.progress", data={"run_id": 1, "visited": 1}, user_id=7)
    second = Event(type="run.progress", data={"run_id": 1, "visited": 2}, user_id=7)
    other = Event(type="run.progress", data={"run_id": 2, "visited": 9}, user_id=8)
    for event in (first, other, second, Event(type="campaign.tick", user_id=7)):
        bus.publish(event)  # with no subscriber, too
    assert bus.latest("run.progress", 7) is second
    assert bus.latest("run.progress", 8) is other
    assert bus.latest("campaign.tick", 8) is None
