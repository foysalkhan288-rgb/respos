"""In-process pub/sub bus for KDS (kitchen display system) events.

Producers (e.g. tools.dispatch_kds) publish events; WebSocket handlers
subscribe and forward them to connected kitchen displays.
"""

import asyncio
from typing import Any, Dict, Set

_subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = set()


def subscribe() -> "asyncio.Queue[Dict[str, Any]]":
    """Register a new subscriber; returns a queue that receives every event."""
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=200)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: "asyncio.Queue[Dict[str, Any]]") -> None:
    _subscribers.discard(queue)


async def publish(event: Dict[str, Any]) -> int:
    """Deliver an event to all subscribers. Returns the subscriber count."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            continue
    return len(_subscribers)


def subscriber_count() -> int:
    return len(_subscribers)
