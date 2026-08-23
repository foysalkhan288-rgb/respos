"""WebSocket push channel for the Kitchen Display System (KDS).

KDS clients connect to /ws/kds and receive JSON events as orders are
dispatched to the kitchen, replacing client-side polling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class KDSConnectionManager:
    """Tracks connected KDS clients and broadcasts dispatch events."""

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

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

    async def broadcast(self, payload: Dict[str, Any]) -> int:
        """Send a JSON event to every connected client.

        Stale connections are dropped on send failure. Returns the number of
        clients the payload was delivered to (0 when nobody is listening).
        """
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


manager = KDSConnectionManager()
