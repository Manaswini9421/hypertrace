"""Live alert fan-out for the dashboard's WebSocket feed (doc 5.3's
`WS /api/v1/stream/alerts`, so the UI never has to poll — doc Section 6.2).

pika is blocking and thread-based while FastAPI is asyncio, so a background
thread consumes anomaly.flagged and hands each message to the event loop via
`call_soon_threadsafe`; connected WebSockets are then served from asyncio
queues. Each client gets its own bounded queue, and a client too slow to
keep up drops messages rather than growing memory without limit — a stalled
browser tab must not become a backpressure problem for the whole service
(doc NFR-3, degrade gracefully).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from hypertrace_common.messaging import ROUTING_KEY_ANOMALY, RabbitMQClient

logger = logging.getLogger("alert-stream")

MAX_QUEUED_ALERTS_PER_CLIENT = 100


class AlertBroadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUED_ALERTS_PER_CLIENT)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def _fan_out(self, message: dict[str, Any]) -> None:
        """Runs on the event loop thread."""
        with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("alert dropped: subscriber queue full (slow client)")

    def _on_message(self, message: dict[str, Any]) -> None:
        """Runs on the pika consumer thread."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._fan_out, message)

    def _consume_forever(self) -> None:
        while True:
            try:
                RabbitMQClient().consume(
                    queue="api-bff.alerts",
                    routing_keys=[ROUTING_KEY_ANOMALY],
                    on_message=self._on_message,
                )
            except Exception:
                logger.exception("alert consumer died, restarting")

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread = threading.Thread(target=self._consume_forever, daemon=True, name="alert-consumer")
        self._thread.start()
