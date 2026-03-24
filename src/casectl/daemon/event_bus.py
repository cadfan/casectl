"""Async event bus for inter-plugin communication and WebSocket broadcast.

The :class:`EventBus` is the backbone of casectl's reactive architecture.
Plugins subscribe handlers to named events (e.g. ``"temperature.changed"``),
and any component can emit events that fan out to all subscribers.

WebSocket connections can also subscribe to receive *all* events as JSON
frames, enabling real-time dashboards and the HTMX web UI.
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventBus:
    """Asynchronous publish/subscribe event bus with WebSocket broadcast.

    Parameters
    ----------
    max_ws:
        Maximum number of simultaneous WebSocket subscribers.  Connections
        beyond this limit are rejected by :meth:`add_ws_subscriber`.
    """

    def __init__(self, max_ws: int = 10) -> None:
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._ws_subscribers: set[Any] = set()  # WebSocket instances
        self._max_ws: int = max_ws

    # ------------------------------------------------------------------
    # Handler subscription
    # ------------------------------------------------------------------

    def subscribe(self, event: str, handler: Callable[..., Any]) -> None:
        """Register *handler* to be called when *event* is emitted.

        Parameters
        ----------
        event:
            Event name (e.g. ``"fan.speed_changed"``).
        handler:
            An async or sync callable that accepts a single *data* argument.
            Async handlers are awaited; sync handlers are called directly.
        """
        if event not in self._handlers:
            self._handlers[event] = []

        if handler in self._handlers[event]:
            logger.debug(
                "Handler %r already subscribed to %r — skipping duplicate",
                getattr(handler, "__name__", repr(handler)),
                event,
            )
            return

        self._handlers[event].append(handler)
        logger.debug(
            "Subscribed %r to event %r",
            getattr(handler, "__name__", repr(handler)),
            event,
        )

    def unsubscribe(self, event: str, handler: Callable[..., Any]) -> None:
        """Remove *handler* from *event*.

        Silently does nothing if the handler is not subscribed.
        """
        handlers = self._handlers.get(event)
        if handlers is None:
            return

        try:
            handlers.remove(handler)
            logger.debug(
                "Unsubscribed %r from event %r",
                getattr(handler, "__name__", repr(handler)),
                event,
            )
        except ValueError:
            pass

        # Clean up empty handler lists.
        if not handlers:
            del self._handlers[event]

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    async def emit(self, event: str, data: Any = None) -> None:
        """Emit *event*, calling all registered handlers and broadcasting to WebSocket subscribers.

        Each handler is invoked inside its own ``try / except`` so that a
        failing handler never prevents other handlers or the WebSocket
        broadcast from executing.

        Parameters
        ----------
        event:
            The event name.
        data:
            Arbitrary payload passed to handlers and serialised as JSON for
            WebSocket subscribers.
        """
        import asyncio
        import inspect

        # -- 1. Dispatch to registered handlers ----------------------------

        handlers = self._handlers.get(event, [])
        for handler in list(handlers):  # copy to allow mutation during iteration
            handler_name = getattr(handler, "__name__", repr(handler))
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    # Run sync handlers; wrap in to_thread if they might block.
                    handler(data)
            except Exception:
                logger.error(
                    "Handler %r failed for event %r:\n%s",
                    handler_name,
                    event,
                    traceback.format_exc(),
                )
                # Continue to next handler — never propagate.

        # -- 2. Broadcast to WebSocket subscribers -------------------------

        if not self._ws_subscribers:
            return

        message = self._build_ws_message(event, data)
        if message is None:
            return

        # Iterate over a copy — subscribers may be removed during iteration.
        stale: list[Any] = []
        for ws in list(self._ws_subscribers):
            try:
                await ws.send_text(message)
            except Exception as exc:
                # Attempt to detect WebSocketDisconnect by type name so we
                # don't need to import starlette at module level.
                exc_type_name = type(exc).__name__
                if exc_type_name == "WebSocketDisconnect":
                    logger.debug("WebSocket subscriber disconnected during broadcast")
                else:
                    logger.warning(
                        "Error broadcasting event %r to WebSocket subscriber: %s",
                        event,
                        exc,
                    )
                stale.append(ws)

        for ws in stale:
            self._ws_subscribers.discard(ws)

    # ------------------------------------------------------------------
    # WebSocket management
    # ------------------------------------------------------------------

    def add_ws_subscriber(self, ws: Any) -> bool:
        """Add a WebSocket connection to the broadcast set.

        Parameters
        ----------
        ws:
            A WebSocket connection that exposes an async ``send_text`` method
            (e.g. a Starlette / FastAPI ``WebSocket``).

        Returns
        -------
        bool
            ``True`` if the subscriber was added, ``False`` if the maximum
            number of WebSocket subscribers has been reached.
        """
        if len(self._ws_subscribers) >= self._max_ws:
            logger.warning(
                "WebSocket subscriber rejected — at capacity (%d/%d)",
                len(self._ws_subscribers),
                self._max_ws,
            )
            return False

        self._ws_subscribers.add(ws)
        logger.debug(
            "WebSocket subscriber added (%d/%d)",
            len(self._ws_subscribers),
            self._max_ws,
        )
        return True

    def remove_ws_subscriber(self, ws: Any) -> None:
        """Remove a WebSocket connection from the broadcast set.

        Silently does nothing if the connection is not in the set.
        """
        self._ws_subscribers.discard(ws)
        logger.debug(
            "WebSocket subscriber removed (%d/%d)",
            len(self._ws_subscribers),
            self._max_ws,
        )

    @property
    def ws_count(self) -> int:
        """Number of currently connected WebSocket subscribers."""
        return len(self._ws_subscribers)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_ws_message(event: str, data: Any) -> str | None:
        """Serialise an event + data payload to a JSON string.

        Returns ``None`` if serialisation fails (e.g. data contains
        non-serialisable objects).
        """
        try:
            return json.dumps({"type": event, "data": data}, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Failed to serialise event %r for WebSocket broadcast: %s",
                event,
                exc,
            )
            return None
