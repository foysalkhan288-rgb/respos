"""WebSocket push channel for the Kitchen Display System (KDS).

KDS clients connect to /ws/kds and receive JSON events as orders are
dispatched to the kitchen, replacing client-side polling.

When REDIS_URL is configured, dispatch events are also published to a Redis
channel so every uvicorn worker relays events to its locally connected KDS
clients (multi-worker fan-out). Without Redis each worker only reaches its
own websocket connections.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")
REDIS_CHANNEL = "cafe_os:kds_events"


class KDSConnectionManager:
    """Tracks connected KDS clients and broadcasts dispatch events."""

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._redis_publish_client = None
        self.worker_id = uuid.uuid4().hex

    @property
    def redis_enabled(self) -> bool:
        return self._redis_publish_client is not None

    def configure_redis(self, client) -> None:
        self._redis_publish_client = client

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def ping_redis(self) -> Optional[bool]:
        if self._redis_publish_client is None:
            return None
        try:
            await self._redis_publish_client.ping()
            return True
        except Exception:
            return False

    async def _deliver_local(self, payload: Dict[str, Any]) -> int:
        async with self._lock:
            targets = list(self._connections)
        delivered = 0
        for websocket in targets:
            try:
                await websocket.send_json(payload)
                delivered += 1
            except Exception:  # pragma: no cover - transport-level failure
                logger.warning("KDS websocket send failed; dropping connection")
                await self.disconnect(websocket)
        return delivered

    async def broadcast(self, payload: Dict[str, Any]) -> int:
        """Deliver an event to local clients and, when configured, publish it
        to Redis for the other workers. Stale local connections are dropped.

        Returns the number of local clients the payload was delivered to.
        """
        delivered = await self._deliver_local(payload)
        if self._redis_publish_client is not None:
            try:
                envelope = json.dumps({"origin": self.worker_id, "event": payload})
                await self._redis_publish_client.publish(REDIS_CHANNEL, envelope)
            except Exception:  # pragma: no cover - broker outage must not break orders
                logger.warning("KDS redis publish failed; continuing with local delivery")
        return delivered


manager = KDSConnectionManager()


class RedisRelayHandle:
    def __init__(self, client, pubsub, task: asyncio.Task):
        self.client = client
        self.pubsub = pubsub
        self.task = task


async def start_redis_relay(url: Optional[str] = None) -> RedisRelayHandle:
    """Subscribe this worker to the shared KDS channel and relay remote
    events to locally connected websockets."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(url or REDIS_URL, decode_responses=True)
    manager.configure_redis(client)
    pubsub = client.pubsub()
    await pubsub.subscribe(REDIS_CHANNEL)

    async def _listen() -> None:
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    envelope = json.loads(message.get("data"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(envelope, dict):
                    continue
                if envelope.get("origin") == manager.worker_id:
                    continue  # already delivered locally by broadcast()
                event = envelope.get("event")
                if isinstance(event, dict):
                    await manager._deliver_local(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - listener failure is non-fatal
            logger.exception("KDS redis relay stopped")

    task = asyncio.create_task(_listen())
    return RedisRelayHandle(client=client, pubsub=pubsub, task=task)


async def stop_redis_relay(handle: RedisRelayHandle) -> None:
    handle.task.cancel()
    try:
        await handle.task
    except asyncio.CancelledError:
        pass
    for closer in (handle.pubsub.unsubscribe, handle.pubsub.aclose, handle.client.aclose):
        try:
            result = closer(REDIS_CHANNEL) if closer is handle.pubsub.unsubscribe else closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # pragma: no cover - best-effort shutdown
            pass
    manager.configure_redis(None)
