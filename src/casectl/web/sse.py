"""Server-Sent Events (SSE) endpoint for real-time dashboard updates.

Bridges the :class:`~casectl.daemon.event_bus.EventBus` to browser clients
via the SSE protocol.  When any plugin emits a state-change event, connected
SSE clients receive a push notification within milliseconds, enabling the
HTMX dashboard to refresh the affected partial immediately rather than
waiting for the next polling interval.

This achieves the <500 ms round-trip target for dashboard state reflection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from casectl.daemon.event_bus import EventBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Events that should trigger dashboard partial refreshes.
# Maps event type prefixes to the SSE event name that the browser listens for.
_REFRESH_TRIGGERS: dict[str, str] = {
    "fan.": "refresh:fan",
    "led.": "refresh:led",
    "oled.": "refresh:oled",
    "monitor.": "refresh:monitor",
    "config.": "refresh:all",
    "temperature.": "refresh:monitor",
    "metrics.": "refresh:monitor",
    "state.": "refresh:all",
}

# Maximum number of concurrent SSE clients.
_MAX_SSE_CLIENTS = 20

# Heartbeat interval in seconds (keeps connection alive through proxies).
_HEARTBEAT_INTERVAL = 15.0


# ---------------------------------------------------------------------------
# SSE client tracker
# ---------------------------------------------------------------------------


class SSEClientManager:
    """Track active SSE client queues and broadcast events to them.

    Each connected client gets an :class:`asyncio.Queue` that receives
    ``(event_name, data_json)`` tuples.  The :meth:`broadcast` method
    fans out a message to all connected clients.

    Parameters
    ----------
    max_clients:
        Maximum number of simultaneous SSE connections.
    """

    def __init__(self, max_clients: int = _MAX_SSE_CLIENTS) -> None:
        self._queues: set[asyncio.Queue[tuple[str, str] | None]] = set()
        self._max_clients = max_clients

    @property
    def client_count(self) -> int:
        """Number of currently connected SSE clients."""
        return len(self._queues)

    def add_client(self) -> asyncio.Queue[tuple[str, str] | None] | None:
        """Create and return a new client queue, or ``None`` if at capacity."""
        if len(self._queues) >= self._max_clients:
            logger.warning(
                "SSE client rejected — at capacity (%d/%d)",
                len(self._queues),
                self._max_clients,
            )
            return None
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=64)
        self._queues.add(queue)
        logger.debug("SSE client added (%d/%d)", len(self._queues), self._max_clients)
        return queue

    def remove_client(self, queue: asyncio.Queue[tuple[str, str] | None]) -> None:
        """Remove a client queue from the broadcast set."""
        self._queues.discard(queue)
        logger.debug("SSE client removed (%d/%d)", len(self._queues), self._max_clients)

    async def broadcast(self, event_name: str, data: str) -> None:
        """Send an event to all connected SSE clients.

        Clients whose queues are full have the oldest message dropped
        (backpressure) rather than blocking the broadcaster.
        """
        stale: list[asyncio.Queue[tuple[str, str] | None]] = []
        for queue in list(self._queues):
            try:
                queue.put_nowait((event_name, data))
            except asyncio.QueueFull:
                # Drop oldest and retry — client is slow.
                try:
                    queue.get_nowait()
                    queue.put_nowait((event_name, data))
                except Exception:
                    stale.append(queue)
        for q in stale:
            self._queues.discard(q)

    async def shutdown(self) -> None:
        """Signal all clients to disconnect by sending ``None``."""
        for queue in list(self._queues):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


# ---------------------------------------------------------------------------
# EventBus → SSE bridge
# ---------------------------------------------------------------------------


def _classify_event(event_type: str) -> str | None:
    """Map an EventBus event type to an SSE event name.

    Returns ``None`` if the event should not trigger an SSE push.
    """
    for prefix, sse_event in _REFRESH_TRIGGERS.items():
        if event_type.startswith(prefix):
            return sse_event
    return None


def create_event_bridge(event_bus: EventBus, sse_manager: SSEClientManager) -> None:
    """Subscribe to the EventBus and bridge relevant events to SSE clients.

    This installs per-event async handlers on the EventBus that convert
    incoming events into SSE push messages with <1 ms overhead.
    """
    # Known events that should trigger dashboard refreshes.
    _BRIDGE_EVENTS = [
        "fan.mode_changed", "fan.speed_changed", "fan.duty_changed",
        "led.mode_changed", "led.color_changed",
        "oled.screen_changed", "oled.rotation_changed",
        "monitor.metrics_updated", "monitor.threshold_crossed",
        "config.updated", "config.changed",
        "temperature.changed",
        "metrics.updated",
        "state.changed",
    ]

    for evt in _BRIDGE_EVENTS:
        sse_evt = _classify_event(evt)
        if sse_evt is None:
            continue

        # Build handler with closure over the event name.
        def _make_bridge_handler(event_name: str, sse_event_name: str):
            async def _handler(data: Any) -> None:
                payload = json.dumps({
                    "event": event_name,
                    "data": data,
                    "ts": time.time(),
                }, default=str)
                await sse_manager.broadcast(sse_event_name, payload)
            _handler.__name__ = f"sse_bridge_{event_name}"
            return _handler

        handler = _make_bridge_handler(evt, sse_evt)
        event_bus.subscribe(evt, handler)
        logger.debug("SSE bridge: %s -> %s", evt, sse_evt)


# ---------------------------------------------------------------------------
# SSE streaming generator
# ---------------------------------------------------------------------------


async def _sse_stream(
    queue: asyncio.Queue[tuple[str, str] | None],
    sse_manager: SSEClientManager,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted messages from the client queue.

    Sends a heartbeat comment every ``_HEARTBEAT_INTERVAL`` seconds to
    keep the connection alive through reverse proxies and load balancers.
    """
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                # Send heartbeat comment to keep connection alive.
                yield ": heartbeat\n\n"
                continue

            if msg is None:
                # Shutdown signal.
                break

            event_name, data = msg
            yield f"event: {event_name}\ndata: {data}\n\n"
    finally:
        sse_manager.remove_client(queue)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_sse_router(event_bus: EventBus) -> tuple[APIRouter, SSEClientManager]:
    """Create the SSE router and its client manager.

    Parameters
    ----------
    event_bus:
        The :class:`~casectl.daemon.event_bus.EventBus` to bridge events from.

    Returns
    -------
    tuple[APIRouter, SSEClientManager]
        The router to mount and the manager for lifecycle control.
    """
    router = APIRouter()
    sse_manager = SSEClientManager()

    # Wire up the EventBus → SSE bridge.
    create_event_bridge(event_bus, sse_manager)

    @router.get("/api/sse", tags=["realtime"])
    async def sse_endpoint(request: Request) -> StreamingResponse:
        """Server-Sent Events stream for real-time dashboard updates.

        Clients receive push events when plugin state changes, enabling
        immediate partial refreshes without polling.

        Event types:
        - ``refresh:fan`` — fan mode or speed changed
        - ``refresh:led`` — LED mode or colour changed
        - ``refresh:oled`` — OLED screen or rotation changed
        - ``refresh:monitor`` — system metrics updated
        - ``refresh:all`` — config or state changed (refresh everything)

        Each event's ``data`` field is a JSON object with:
        - ``event``: the original EventBus event name
        - ``data``: the event payload
        - ``ts``: server timestamp (Unix epoch seconds)
        """
        queue = sse_manager.add_client()
        if queue is None:
            return StreamingResponse(
                iter(["event: error\ndata: Too many connections\n\n"]),
                media_type="text/event-stream",
                status_code=503,
            )

        return StreamingResponse(
            _sse_stream(queue, sse_manager),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    return router, sse_manager
