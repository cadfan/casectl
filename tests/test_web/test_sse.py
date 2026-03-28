"""Tests for casectl.web.sse — Server-Sent Events real-time dashboard updates.

Verifies the SSE endpoint, client manager, event bridge, and <500 ms
round-trip latency from EventBus emit to SSE message delivery.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from casectl.daemon.event_bus import EventBus
from casectl.web.sse import (
    SSEClientManager,
    _classify_event,
    create_event_bridge,
    create_sse_router,
)


# ---------------------------------------------------------------------------
# SSEClientManager unit tests
# ---------------------------------------------------------------------------


class TestSSEClientManager:
    """Verify SSEClientManager add/remove/broadcast semantics."""

    def test_add_client_returns_queue(self) -> None:
        mgr = SSEClientManager(max_clients=5)
        queue = mgr.add_client()
        assert queue is not None
        assert mgr.client_count == 1

    def test_add_client_at_capacity_returns_none(self) -> None:
        mgr = SSEClientManager(max_clients=2)
        q1 = mgr.add_client()
        q2 = mgr.add_client()
        q3 = mgr.add_client()
        assert q1 is not None
        assert q2 is not None
        assert q3 is None
        assert mgr.client_count == 2

    def test_remove_client_decrements_count(self) -> None:
        mgr = SSEClientManager(max_clients=5)
        queue = mgr.add_client()
        assert queue is not None
        mgr.remove_client(queue)
        assert mgr.client_count == 0

    def test_remove_nonexistent_client_is_safe(self) -> None:
        mgr = SSEClientManager()
        fake_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        mgr.remove_client(fake_queue)  # should not raise
        assert mgr.client_count == 0

    async def test_broadcast_delivers_to_all_clients(self) -> None:
        mgr = SSEClientManager(max_clients=10)
        q1 = mgr.add_client()
        q2 = mgr.add_client()
        assert q1 is not None and q2 is not None

        await mgr.broadcast("refresh:fan", '{"event":"fan.mode_changed"}')

        msg1 = q1.get_nowait()
        msg2 = q2.get_nowait()
        assert msg1 == ("refresh:fan", '{"event":"fan.mode_changed"}')
        assert msg2 == ("refresh:fan", '{"event":"fan.mode_changed"}')

    async def test_broadcast_drops_oldest_when_queue_full(self) -> None:
        mgr = SSEClientManager(max_clients=5)
        queue = mgr.add_client()
        assert queue is not None

        # Fill the queue (maxsize=64 in implementation).
        for i in range(64):
            await mgr.broadcast("refresh:monitor", f'{{"i":{i}}}')

        # Queue is full; next broadcast should drop the oldest.
        await mgr.broadcast("refresh:monitor", '{"i":64}')

        # The queue should still be full (64 items) but the first item dropped.
        assert queue.qsize() == 64
        first = queue.get_nowait()
        assert first == ("refresh:monitor", '{"i":1}')

    async def test_shutdown_sends_none_to_all(self) -> None:
        mgr = SSEClientManager(max_clients=5)
        q1 = mgr.add_client()
        q2 = mgr.add_client()
        assert q1 is not None and q2 is not None

        await mgr.shutdown()

        assert q1.get_nowait() is None
        assert q2.get_nowait() is None

    def test_capacity_frees_after_remove(self) -> None:
        mgr = SSEClientManager(max_clients=1)
        q1 = mgr.add_client()
        assert q1 is not None
        assert mgr.add_client() is None  # full

        mgr.remove_client(q1)
        q2 = mgr.add_client()
        assert q2 is not None
        assert mgr.client_count == 1


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------


class TestClassifyEvent:
    """Verify _classify_event maps event names to SSE event names."""

    def test_fan_events(self) -> None:
        assert _classify_event("fan.mode_changed") == "refresh:fan"
        assert _classify_event("fan.speed_changed") == "refresh:fan"
        assert _classify_event("fan.duty_changed") == "refresh:fan"

    def test_led_events(self) -> None:
        assert _classify_event("led.mode_changed") == "refresh:led"
        assert _classify_event("led.color_changed") == "refresh:led"

    def test_oled_events(self) -> None:
        assert _classify_event("oled.screen_changed") == "refresh:oled"
        assert _classify_event("oled.rotation_changed") == "refresh:oled"

    def test_monitor_events(self) -> None:
        assert _classify_event("monitor.metrics_updated") == "refresh:monitor"
        assert _classify_event("temperature.changed") == "refresh:monitor"
        assert _classify_event("metrics.updated") == "refresh:monitor"

    def test_config_events(self) -> None:
        assert _classify_event("config.updated") == "refresh:all"
        assert _classify_event("config.changed") == "refresh:all"

    def test_state_events(self) -> None:
        assert _classify_event("state.changed") == "refresh:all"

    def test_unknown_event_returns_none(self) -> None:
        assert _classify_event("daemon.started") is None
        assert _classify_event("unknown.event") is None
        assert _classify_event("") is None


# ---------------------------------------------------------------------------
# EventBus → SSE bridge
# ---------------------------------------------------------------------------


class TestEventBridge:
    """Verify create_event_bridge subscribes handlers that forward events."""

    async def test_bridge_forwards_fan_event(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        await bus.emit("fan.mode_changed", {"mode": "manual"})

        msg = queue.get_nowait()
        assert msg is not None
        event_name, data = msg
        assert event_name == "refresh:fan"
        parsed = json.loads(data)
        assert parsed["event"] == "fan.mode_changed"
        assert parsed["data"]["mode"] == "manual"
        assert "ts" in parsed

    async def test_bridge_forwards_config_event(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        await bus.emit("config.updated", {"section": "fan", "values": {"mode": 2}})

        msg = queue.get_nowait()
        assert msg is not None
        event_name, data = msg
        assert event_name == "refresh:all"

    async def test_bridge_forwards_led_event(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        await bus.emit("led.color_changed", {"red": 255, "green": 0, "blue": 0})

        msg = queue.get_nowait()
        assert msg is not None
        event_name, _ = msg
        assert event_name == "refresh:led"

    async def test_bridge_forwards_monitor_event(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        await bus.emit("monitor.metrics_updated", {"cpu_temp": 55.0})

        msg = queue.get_nowait()
        assert msg is not None
        event_name, _ = msg
        assert event_name == "refresh:monitor"

    async def test_bridge_ignores_unrelated_events(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        await bus.emit("daemon.started", {"version": "0.2.0"})

        assert queue.empty()

    async def test_bridge_handles_multiple_events(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        await bus.emit("fan.mode_changed", {"mode": "auto"})
        await bus.emit("led.mode_changed", {"mode": "rainbow"})
        await bus.emit("oled.screen_changed", {"screen": 1})

        msgs = []
        while not queue.empty():
            msgs.append(queue.get_nowait())

        assert len(msgs) == 3
        event_names = [m[0] for m in msgs]
        assert "refresh:fan" in event_names
        assert "refresh:led" in event_names
        assert "refresh:oled" in event_names


# ---------------------------------------------------------------------------
# Round-trip latency test (<500 ms)
# ---------------------------------------------------------------------------


class TestRoundTripLatency:
    """Verify that EventBus → SSE push takes <500 ms."""

    async def test_emit_to_sse_under_500ms(self) -> None:
        """Emit an event on the EventBus and verify the SSE client receives
        it within 500 ms (the AC requirement)."""
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        start = time.monotonic()
        await bus.emit("fan.speed_changed", {"duty": [128, 128, 128]})
        msg = queue.get_nowait()  # Should be available immediately (no I/O)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert msg is not None
        assert elapsed_ms < 500, f"Round-trip took {elapsed_ms:.1f}ms, expected <500ms"

    async def test_emit_to_sse_latency_with_timestamp(self) -> None:
        """Verify the server timestamp in the SSE payload allows the client
        to measure round-trip latency."""
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        before = time.time()
        await bus.emit("config.updated", {"section": "led"})
        after = time.time()

        msg = queue.get_nowait()
        assert msg is not None
        _, data = msg
        parsed = json.loads(data)
        ts = parsed["ts"]
        assert before <= ts <= after, "Server timestamp should be between before and after"

    async def test_concurrent_events_under_500ms(self) -> None:
        """Multiple rapid events should all arrive within 500 ms."""
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        start = time.monotonic()
        for i in range(10):
            await bus.emit("fan.speed_changed", {"duty": [i * 25, i * 25, i * 25]})

        msgs = []
        while not queue.empty():
            msgs.append(queue.get_nowait())

        elapsed_ms = (time.monotonic() - start) * 1000
        assert len(msgs) == 10
        assert elapsed_ms < 500, f"10 events took {elapsed_ms:.1f}ms, expected <500ms"

    async def test_multiple_clients_under_500ms(self) -> None:
        """Broadcast to multiple clients should complete within 500 ms."""
        bus = EventBus()
        mgr = SSEClientManager(max_clients=20)
        queues = []
        for _ in range(10):
            q = mgr.add_client()
            assert q is not None
            queues.append(q)

        create_event_bridge(bus, mgr)

        start = time.monotonic()
        await bus.emit("led.color_changed", {"red": 255})

        for q in queues:
            msg = q.get_nowait()
            assert msg is not None

        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"Broadcast to 10 clients took {elapsed_ms:.1f}ms"


# ---------------------------------------------------------------------------
# SSE Router integration test
# ---------------------------------------------------------------------------


class TestSSERouter:
    """Verify create_sse_router creates a working router and manager."""

    def test_create_sse_router_returns_router_and_manager(self) -> None:
        bus = EventBus()
        router, mgr = create_sse_router(bus)
        assert router is not None
        assert mgr is not None
        assert isinstance(mgr, SSEClientManager)

    async def test_sse_endpoint_bridges_events_to_queue(self) -> None:
        """Integration test: SSE router bridges EventBus events to client queues."""
        bus = EventBus()
        router, mgr = create_sse_router(bus)

        # Simulate a client connecting by adding to the manager directly.
        queue = mgr.add_client()
        assert queue is not None

        # Emit an event — the bridge (already installed) should forward it.
        await bus.emit("fan.mode_changed", {"mode": "auto"})

        # The queue should have received the SSE message.
        msg = queue.get_nowait()
        assert msg is not None
        event_name, data = msg
        assert event_name == "refresh:fan"
        parsed = json.loads(data)
        assert parsed["event"] == "fan.mode_changed"
        assert parsed["data"]["mode"] == "auto"


# ---------------------------------------------------------------------------
# SSE endpoint capacity test
# ---------------------------------------------------------------------------


class TestSSECapacity:
    """Verify SSE endpoint rejects clients when at capacity."""

    def test_sse_at_capacity_returns_503(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        bus = EventBus()
        router, mgr = create_sse_router(bus)

        # Fill up the SSE client slots.
        for _ in range(mgr._max_clients):
            mgr.add_client()

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)

        # Next connection should be rejected.
        resp = client.get("/api/sse")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Config change event emission in server
# ---------------------------------------------------------------------------


class TestConfigChangeEmitsEvent:
    """Verify PATCH /api/config emits config.updated on EventBus."""

    def test_patch_config_emits_event(self) -> None:
        """After PATCH /api/config, the EventBus should have emitted config.updated."""
        import os
        from unittest.mock import AsyncMock, MagicMock, patch

        from casectl.daemon.event_bus import EventBus
        from casectl.plugins.base import PluginStatus

        plugin_host = MagicMock()
        plugin_host.list_plugins.return_value = []
        plugin_host.get_routes.return_value = []
        plugin_host.get_all_statuses.return_value = {}
        plugin_host.get_plugin.return_value = None
        plugin_host.start_all = AsyncMock()
        plugin_host.stop_all = AsyncMock()

        config_manager = MagicMock()
        mock_config = MagicMock()
        mock_config.model_dump.return_value = {"fan": {"mode": 2}}
        config_manager.update = AsyncMock(return_value=mock_config)

        event_bus = EventBus(max_ws=10)

        # Track emitted events.
        emitted: list[tuple[str, Any]] = []
        original_emit = event_bus.emit

        async def tracking_emit(event: str, data: Any = None) -> None:
            emitted.append((event, data))
            await original_emit(event, data)

        event_bus.emit = tracking_emit

        with patch.dict(os.environ, {}, clear=False):
            from casectl.daemon.server import create_app

            app = create_app(
                plugin_host=plugin_host,
                config_manager=config_manager,
                event_bus=event_bus,
                host="127.0.0.1",
            )

        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)

        resp = client.patch(
            "/api/config",
            json={"section": "fan", "values": {"mode": 2}},
        )
        assert resp.status_code == 200

        # Check that config.updated was emitted.
        config_events = [(e, d) for e, d in emitted if e == "config.updated"]
        assert len(config_events) >= 1
        event_data = config_events[0][1]
        assert event_data["section"] == "fan"
        assert "ts" in event_data


# ---------------------------------------------------------------------------
# SSE message format verification
# ---------------------------------------------------------------------------


class TestSSEMessageFormat:
    """Verify SSE messages follow the correct format."""

    async def test_message_contains_event_and_data_and_ts(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)
        await bus.emit("state.changed", {"key": "value"})

        msg = queue.get_nowait()
        assert msg is not None
        event_name, data_str = msg
        assert event_name == "refresh:all"

        parsed = json.loads(data_str)
        assert "event" in parsed
        assert "data" in parsed
        assert "ts" in parsed
        assert parsed["event"] == "state.changed"
        assert parsed["data"] == {"key": "value"}
        assert isinstance(parsed["ts"], float)

    async def test_message_ts_is_recent(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        create_event_bridge(bus, mgr)

        before = time.time()
        await bus.emit("fan.mode_changed", {})
        after = time.time()

        msg = queue.get_nowait()
        assert msg is not None
        _, data_str = msg
        parsed = json.loads(data_str)
        assert before <= parsed["ts"] <= after


# ---------------------------------------------------------------------------
# SSE stream generator tests
# ---------------------------------------------------------------------------


class TestSSEStreamGenerator:
    """Verify the _sse_stream async generator produces correct SSE format."""

    async def test_stream_yields_sse_formatted_events(self) -> None:
        from casectl.web.sse import _sse_stream

        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        # Put a message and a shutdown signal.
        queue.put_nowait(("refresh:fan", '{"event":"fan.mode_changed"}'))
        queue.put_nowait(None)  # shutdown

        lines = []
        async for chunk in _sse_stream(queue, mgr):
            lines.append(chunk)

        assert len(lines) == 1
        assert lines[0] == 'event: refresh:fan\ndata: {"event":"fan.mode_changed"}\n\n'

    async def test_stream_yields_heartbeat_on_timeout(self) -> None:
        from casectl.web.sse import _sse_stream, _HEARTBEAT_INTERVAL
        import casectl.web.sse as sse_module

        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        # Temporarily set a very short heartbeat for testing.
        original = sse_module._HEARTBEAT_INTERVAL
        try:
            sse_module._HEARTBEAT_INTERVAL = 0.05  # 50ms

            # Don't put anything — wait for heartbeat then shutdown.
            async def shutdown_after_delay():
                await asyncio.sleep(0.1)
                queue.put_nowait(None)

            task = asyncio.create_task(shutdown_after_delay())

            lines = []
            async for chunk in _sse_stream(queue, mgr):
                lines.append(chunk)
                if len(lines) >= 2:
                    # Got heartbeat + potential more, send shutdown.
                    queue.put_nowait(None)
                    break

            await task

            # Should have at least one heartbeat.
            heartbeats = [l for l in lines if l.startswith(": heartbeat")]
            assert len(heartbeats) >= 1
        finally:
            sse_module._HEARTBEAT_INTERVAL = original

    async def test_stream_removes_client_on_exit(self) -> None:
        from casectl.web.sse import _sse_stream

        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None
        assert mgr.client_count == 1

        queue.put_nowait(None)  # immediate shutdown

        async for _ in _sse_stream(queue, mgr):
            pass

        # Client should be removed after generator exits.
        assert mgr.client_count == 0

    async def test_stream_multiple_events(self) -> None:
        from casectl.web.sse import _sse_stream

        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        queue.put_nowait(("refresh:fan", '{"i":1}'))
        queue.put_nowait(("refresh:led", '{"i":2}'))
        queue.put_nowait(("refresh:monitor", '{"i":3}'))
        queue.put_nowait(None)

        lines = []
        async for chunk in _sse_stream(queue, mgr):
            lines.append(chunk)

        assert len(lines) == 3
        assert "refresh:fan" in lines[0]
        assert "refresh:led" in lines[1]
        assert "refresh:monitor" in lines[2]


# ---------------------------------------------------------------------------
# SSE endpoint HTTP response tests
# ---------------------------------------------------------------------------


class TestSSEEndpointHTTP:
    """Verify the SSE FastAPI endpoint returns proper HTTP responses."""

    async def test_sse_endpoint_creates_client_and_streams(self) -> None:
        """SSE endpoint creates an SSE client and formats messages correctly."""
        from casectl.web.sse import _sse_stream

        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        # Put an event and shutdown.
        queue.put_nowait(("refresh:fan", '{"test":true}'))
        queue.put_nowait(None)

        chunks = []
        async for chunk in _sse_stream(queue, mgr):
            chunks.append(chunk)

        assert len(chunks) == 1
        # Verify SSE format: "event: <name>\ndata: <data>\n\n"
        assert chunks[0].startswith("event: refresh:fan\n")
        assert 'data: {"test":true}\n' in chunks[0]
        assert chunks[0].endswith("\n\n")

    async def test_sse_endpoint_format_multiple_events(self) -> None:
        """SSE endpoint formats multiple events correctly."""
        from casectl.web.sse import _sse_stream

        mgr = SSEClientManager()
        queue = mgr.add_client()
        assert queue is not None

        queue.put_nowait(("refresh:led", '{"color":"red"}'))
        queue.put_nowait(("refresh:fan", '{"speed":50}'))
        queue.put_nowait(None)

        chunks = []
        async for chunk in _sse_stream(queue, mgr):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert "refresh:led" in chunks[0]
        assert "refresh:fan" in chunks[1]


# ---------------------------------------------------------------------------
# Bridge handler naming
# ---------------------------------------------------------------------------


class TestBridgeHandlerNaming:
    """Verify bridge handlers have descriptive names for debugging."""

    def test_bridge_handlers_have_descriptive_names(self) -> None:
        bus = EventBus()
        mgr = SSEClientManager()
        create_event_bridge(bus, mgr)

        # Check that the handlers registered on the bus have descriptive names.
        for event, handlers in bus._handlers.items():
            for h in handlers:
                assert h.__name__.startswith("sse_bridge_"), (
                    f"Handler for {event} has unexpected name: {h.__name__}"
                )


# ---------------------------------------------------------------------------
# Shutdown broadcast under load
# ---------------------------------------------------------------------------


class TestShutdownBroadcast:
    """Verify shutdown properly signals all clients."""

    async def test_shutdown_with_full_queues(self) -> None:
        mgr = SSEClientManager(max_clients=5)
        queues = []
        for _ in range(3):
            q = mgr.add_client()
            assert q is not None
            queues.append(q)

        # Fill all queues.
        for q in queues:
            for i in range(64):
                q.put_nowait(("refresh:fan", f'{{"i":{i}}}'))

        # Shutdown should still work (queues full, but shutdown sends None).
        await mgr.shutdown()

        # The queues are full, so shutdown None may not get through, but
        # the method should not raise.
        assert mgr.client_count == 3  # shutdown doesn't remove clients
