"""``/events``: the SSE stream the UI subscribes to once (spec 14.1).

Every :class:`~netkeep.services.events.Event` on the bus becomes one SSE message:
the event name is ``event.type`` and the data is the event's JSON. A ping comment
goes out every 15 seconds so proxies and the browser keep the connection open.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import anyio
from fastapi import APIRouter
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask

from netkeep.services.events import EventBus, Subscription
from netkeep.web.deps import Bus, CurrentUser

router = APIRouter(tags=["events"])

PING_INTERVAL_S = 15
#: How long a stream gets to finish by itself once the server is stopping.
SHUTDOWN_GRACE_S = 2


@router.get(
    "/events",
    operation_id="stream_events",
    response_class=EventSourceResponse,
    summary="Server-sent events: task progress, run status, and health",
    responses={
        200: {
            "description": "An SSE stream; each message is named by the event type.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def stream_events(user: CurrentUser, bus: Bus) -> EventSourceResponse:
    # Subscribe before the response starts so nothing published in between is lost.
    subscription = bus.subscribe()
    # Set by sse-starlette when the server is stopping. Ending the stream ourselves
    # lets the response complete; cancelling it makes uvicorn log "ASGI callable
    # returned without completing response" as an error (#584).
    stopping = anyio.Event()
    return EventSourceResponse(
        _frames(bus, subscription, user.id, stopping),
        ping=PING_INTERVAL_S,
        shutdown_event=stopping,
        shutdown_grace_period=SHUTDOWN_GRACE_S,
        background=BackgroundTask(bus.unsubscribe, subscription),
    )


async def _frames(
    bus: EventBus, subscription: Subscription, user_id: int, stopping: anyio.Event
) -> AsyncIterator[ServerSentEvent]:
    async def end_on_stop() -> None:
        await stopping.wait()
        bus.unsubscribe(subscription)  # the iterator below ends after the backlog

    watcher = asyncio.create_task(end_on_stop())
    try:
        async for event in subscription:
            if event.user_id is not None and event.user_id != user_id:
                continue
            payload = json.dumps(event.to_dict(), separators=(",", ":"), ensure_ascii=False)
            yield ServerSentEvent(data=payload, event=event.type)
    finally:
        watcher.cancel()
        bus.unsubscribe(subscription)
