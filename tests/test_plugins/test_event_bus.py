"""Tests for casectl.daemon.event_bus — EventBus pub/sub and WebSocket broadcast.

These are CRITICAL tests for the reactive core of casectl.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from casectl.daemon.event_bus import EventBus


# ---------------------------------------------------------------------------
# emit calls handlers
# ---------------------------------------------------------------------------


class TestEmitCallsHandlers:
    """Verify that emit dispatches to all subscribed handlers."""

    async def test_sync_handler_called(self) -> None:
        bus = EventBus()
        received: list[Any] = []

        def handler(data: Any) -> None:
            received.append(data)

        bus.subscribe("test.event", handler)
        await bus.emit("test.event", {"key": "value"})

        assert len(received) == 1
        assert received[0] == {"key": "value"}

    async def test_multiple_handlers_called(self) -> None:
        bus = EventBus()
        results: list[str] = []

        def handler_a(data: Any) -> None:
            results.append("a")

        def handler_b(data: Any) -> None:
            results.append("b")

        bus.subscribe("test.event", handler_a)
        bus.subscribe("test.event", handler_b)
        await bus.emit("test.event", None)

        assert "a" in results
        assert "b" in results
        assert len(results) == 2

    async def test_emit_no_handlers(self) -> None:
        """Emitting an event with no subscribers should not raise."""
        bus = EventBus()
        await bus.emit("no.subscribers", {"data": 42})

    async def test_handler_receives_none_data(self) -> None:
        bus = EventBus()
        received: list[Any] = []

        def handler(data: Any) -> None:
            received.append(data)

        bus.subscribe("evt", handler)
        await bus.emit("evt")
        assert received == [None]

    async def test_duplicate_subscribe_ignored(self) -> None:
        """Subscribing the same handler twice should not call it twice."""
        bus = EventBus()
        call_count = 0

        def handler(data: Any) -> None:
            nonlocal call_count
            call_count += 1

        bus.subscribe("evt", handler)
        bus.subscribe("evt", handler)  # duplicate
        await bus.emit("evt", "x")
        assert call_count == 1


# ---------------------------------------------------------------------------
# Handler exception isolation
# ---------------------------------------------------------------------------


class TestHandlerExceptionIsolation:
    """Verify a failing handler does not block other handlers."""

    async def test_exception_does_not_block_others(self) -> None:
        bus = EventBus()
        results: list[str] = []

        def good_handler_before(data: Any) -> None:
            results.append("before")

        def bad_handler(data: Any) -> None:
            raise ValueError("I broke!")

        def good_handler_after(data: Any) -> None:
            results.append("after")

        bus.subscribe("evt", good_handler_before)
        bus.subscribe("evt", bad_handler)
        bus.subscribe("evt", good_handler_after)

        await bus.emit("evt", "test")

        assert "before" in results
        assert "after" in results
        assert len(results) == 2

    async def test_multiple_failing_handlers(self) -> None:
        bus = EventBus()
        survivor_called = False

        def fail_1(data: Any) -> None:
            raise RuntimeError("fail 1")

        def fail_2(data: Any) -> None:
            raise RuntimeError("fail 2")

        def survivor(data: Any) -> None:
            nonlocal survivor_called
            survivor_called = True

        bus.subscribe("evt", fail_1)
        bus.subscribe("evt", fail_2)
        bus.subscribe("evt", survivor)

        await bus.emit("evt", None)
        assert survivor_called is True


# ---------------------------------------------------------------------------
# Async handler support
# ---------------------------------------------------------------------------


class TestAsyncHandlerSupport:
    """Verify async handlers are properly awaited."""

    async def test_async_handler_awaited(self) -> None:
        bus = EventBus()
        received: list[Any] = []

        async def async_handler(data: Any) -> None:
            await asyncio.sleep(0.01)
            received.append(data)

        bus.subscribe("async.evt", async_handler)
        await bus.emit("async.evt", "async_payload")

        assert received == ["async_payload"]

    async def test_mixed_sync_and_async_handlers(self) -> None:
        bus = EventBus()
        results: list[str] = []

        def sync_handler(data: Any) -> None:
            results.append("sync")

        async def async_handler(data: Any) -> None:
            results.append("async")

        bus.subscribe("mixed", sync_handler)
        bus.subscribe("mixed", async_handler)
        await bus.emit("mixed", None)

        assert "sync" in results
        assert "async" in results

    async def test_async_handler_exception_isolated(self) -> None:
        bus = EventBus()
        called = False

        async def bad_async(data: Any) -> None:
            raise RuntimeError("async boom")

        def good_sync(data: Any) -> None:
            nonlocal called
            called = True

        bus.subscribe("evt", bad_async)
        bus.subscribe("evt", good_sync)
        await bus.emit("evt", None)

        assert called is True


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestSubscribeUnsubscribe:
    """Verify handler removal via unsubscribe."""

    async def test_unsubscribe_prevents_calls(self) -> None:
        bus = EventBus()
        called = False

        def handler(data: Any) -> None:
            nonlocal called
            called = True

        bus.subscribe("evt", handler)
        bus.unsubscribe("evt", handler)
        await bus.emit("evt", None)

        assert called is False

    async def test_unsubscribe_nonexistent_handler(self) -> None:
        """Unsubscribing a handler that was never subscribed should not raise."""
        bus = EventBus()

        def handler(data: Any) -> None:
            pass

        bus.unsubscribe("evt", handler)  # should not raise

    async def test_unsubscribe_nonexistent_event(self) -> None:
        """Unsubscribing from an event that has no handlers should not raise."""
        bus = EventBus()

        def handler(data: Any) -> None:
            pass

        bus.unsubscribe("no.such.event", handler)

    async def test_partial_unsubscribe(self) -> None:
        """Unsubscribing one handler leaves others intact."""
        bus = EventBus()
        results: list[str] = []

        def handler_a(data: Any) -> None:
            results.append("a")

        def handler_b(data: Any) -> None:
            results.append("b")

        bus.subscribe("evt", handler_a)
        bus.subscribe("evt", handler_b)
        bus.unsubscribe("evt", handler_a)

        await bus.emit("evt", None)
        assert results == ["b"]

    async def test_unsubscribe_cleans_up_empty_list(self) -> None:
        """After removing the last handler, the event key is removed."""
        bus = EventBus()

        def handler(data: Any) -> None:
            pass

        bus.subscribe("cleanup.test", handler)
        bus.unsubscribe("cleanup.test", handler)
        assert "cleanup.test" not in bus._handlers


# ---------------------------------------------------------------------------
# WebSocket subscriber limit
# ---------------------------------------------------------------------------


class TestWsSubscriberLimit:
    """Verify the max_ws limit is enforced."""

    def test_ws_subscriber_limit_at_capacity(self) -> None:
        """The 11th subscriber (max_ws=10) should be rejected."""
        bus = EventBus(max_ws=10)
        mock_ws_list = [MagicMock() for _ in range(11)]

        results = []
        for ws in mock_ws_list:
            results.append(bus.add_ws_subscriber(ws))

        # First 10 should be accepted
        assert all(results[:10])
        # 11th should be rejected
        assert results[10] is False
        assert bus.ws_count == 10

    def test_ws_subscriber_added(self) -> None:
        bus = EventBus(max_ws=5)
        ws = MagicMock()
        assert bus.add_ws_subscriber(ws) is True
        assert bus.ws_count == 1

    def test_ws_subscriber_removed(self) -> None:
        bus = EventBus()
        ws = MagicMock()
        bus.add_ws_subscriber(ws)
        bus.remove_ws_subscriber(ws)
        assert bus.ws_count == 0

    def test_remove_nonexistent_ws(self) -> None:
        """Removing a WS that was never added should not raise."""
        bus = EventBus()
        ws = MagicMock()
        bus.remove_ws_subscriber(ws)  # no-op

    def test_ws_capacity_frees_after_remove(self) -> None:
        """After removing a subscriber, the slot is freed for new ones."""
        bus = EventBus(max_ws=2)
        ws1 = MagicMock()
        ws2 = MagicMock()
        ws3 = MagicMock()

        bus.add_ws_subscriber(ws1)
        bus.add_ws_subscriber(ws2)
        assert bus.add_ws_subscriber(ws3) is False  # full

        bus.remove_ws_subscriber(ws1)
        assert bus.add_ws_subscriber(ws3) is True  # now room
        assert bus.ws_count == 2


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------


class TestWsBroadcast:
    """Verify WebSocket subscribers receive JSON messages on emit."""

    async def test_ws_receives_json_message(self) -> None:
        bus = EventBus()
        ws = AsyncMock()
        bus.add_ws_subscriber(ws)

        await bus.emit("test.event", {"temp": 42})

        ws.send_text.assert_called_once()
        msg = json.loads(ws.send_text.call_args[0][0])
        assert msg["type"] == "test.event"
        assert msg["data"]["temp"] == 42

    async def test_ws_broadcast_to_multiple(self) -> None:
        bus = EventBus()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        bus.add_ws_subscriber(ws1)
        bus.add_ws_subscriber(ws2)

        await bus.emit("multi", "hello")

        ws1.send_text.assert_called_once()
        ws2.send_text.assert_called_once()

        msg1 = json.loads(ws1.send_text.call_args[0][0])
        msg2 = json.loads(ws2.send_text.call_args[0][0])
        assert msg1["data"] == "hello"
        assert msg2["data"] == "hello"

    async def test_ws_broadcast_json_structure(self) -> None:
        """Verify the JSON payload has 'type' and 'data' keys."""
        bus = EventBus()
        ws = AsyncMock()
        bus.add_ws_subscriber(ws)

        await bus.emit("metrics.update", {"cpu": 50})

        raw = ws.send_text.call_args[0][0]
        parsed = json.loads(raw)
        assert set(parsed.keys()) == {"type", "data"}
        assert parsed["type"] == "metrics.update"
        assert parsed["data"] == {"cpu": 50}


# ---------------------------------------------------------------------------
# WebSocket disconnect during broadcast
# ---------------------------------------------------------------------------


class TestWsDisconnectDuringBroadcast:
    """Verify disconnected WebSocket clients are removed silently."""

    async def test_disconnected_client_removed(self) -> None:
        bus = EventBus()

        good_ws = AsyncMock()
        bad_ws = AsyncMock()
        # Simulate a WebSocketDisconnect by type name
        disconnect_exc = type("WebSocketDisconnect", (Exception,), {})()
        bad_ws.send_text.side_effect = disconnect_exc

        bus.add_ws_subscriber(good_ws)
        bus.add_ws_subscriber(bad_ws)
        assert bus.ws_count == 2

        await bus.emit("test", "data")

        # The bad WS should have been removed
        assert bus.ws_count == 1
        # The good WS should still have received the message
        good_ws.send_text.assert_called_once()

    async def test_generic_exception_removes_ws(self) -> None:
        """Any exception during send_text removes the subscriber."""
        bus = EventBus()
        ws = AsyncMock()
        ws.send_text.side_effect = ConnectionResetError("peer reset")

        bus.add_ws_subscriber(ws)
        await bus.emit("test", "data")

        assert bus.ws_count == 0

    async def test_all_ws_disconnect(self) -> None:
        """If all WebSocket subscribers disconnect, ws_count drops to 0."""
        bus = EventBus()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        ws1.send_text.side_effect = RuntimeError("gone")
        ws2.send_text.side_effect = RuntimeError("gone")

        bus.add_ws_subscriber(ws1)
        bus.add_ws_subscriber(ws2)

        await bus.emit("test", None)
        assert bus.ws_count == 0

    async def test_handlers_still_called_despite_ws_errors(self) -> None:
        """Handler dispatch and WS broadcast are independent; WS errors
        should not prevent handlers from running."""
        bus = EventBus()
        handler_called = False

        def handler(data: Any) -> None:
            nonlocal handler_called
            handler_called = True

        ws = AsyncMock()
        ws.send_text.side_effect = RuntimeError("ws gone")

        bus.subscribe("evt", handler)
        bus.add_ws_subscriber(ws)

        await bus.emit("evt", "data")
        assert handler_called is True


# ---------------------------------------------------------------------------
# Message serialization edge cases
# ---------------------------------------------------------------------------


class TestMessageSerialization:
    """Verify edge cases in the _build_ws_message static method."""

    def test_build_ws_message_simple(self) -> None:
        msg = EventBus._build_ws_message("evt", {"x": 1})
        assert msg is not None
        parsed = json.loads(msg)
        assert parsed == {"type": "evt", "data": {"x": 1}}

    def test_build_ws_message_with_none_data(self) -> None:
        msg = EventBus._build_ws_message("evt", None)
        assert msg is not None
        parsed = json.loads(msg)
        assert parsed["data"] is None

    def test_build_ws_message_uses_default_str(self) -> None:
        """Non-serializable objects should be converted via str()."""
        from datetime import datetime

        msg = EventBus._build_ws_message("evt", {"ts": datetime(2026, 1, 1)})
        assert msg is not None
        parsed = json.loads(msg)
        assert "2026" in parsed["data"]["ts"]
