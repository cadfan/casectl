"""Tests for WebSocket bidirectional command handling and dropdown controls.

Verifies that:
- WebSocket accepts JSON commands and routes them to config_manager.update()
- Fan/LED mode dropdown selects render correctly in HTMX partials
- WebSocket commands have full parity with REST API / CLI
- Invalid commands return structured error responses
- Keep-alive messages are silently ignored
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casectl.daemon.event_bus import EventBus
from casectl.plugins.base import PluginStatus
from casectl.web.app import create_web_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recv_command_result(ws: Any) -> dict[str, Any]:
    """Read messages from WebSocket until we get a command_result or error.

    The server may emit event broadcasts (e.g. fan.mode_changed) before
    the command_result reply.  This helper skips those intermediate messages.
    """
    for _ in range(10):  # safety limit
        raw = ws.receive_text()
        msg = json.loads(raw)
        if msg.get("type") in ("command_result", "error"):
            return msg
    raise AssertionError("Never received command_result or error from WebSocket")


# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_ws_test_app() -> tuple[TestClient, MagicMock, MagicMock, EventBus]:
    """Create a FastAPI app with create_app() for WebSocket testing.

    Returns (TestClient, mock_plugin_host, mock_config_manager, event_bus).
    """
    plugin_host = MagicMock()
    plugin_host.list_plugins.return_value = []
    plugin_host.get_routes.return_value = []
    plugin_host.get_all_statuses.return_value = {}
    plugin_host.get_plugin.return_value = None
    plugin_host.start_all = AsyncMock()
    plugin_host.stop_all = AsyncMock()

    config_manager = MagicMock()
    config_manager.get = AsyncMock(return_value={})
    config_manager.update = AsyncMock(return_value=MagicMock(
        model_dump=MagicMock(return_value={"mode": 0}),
    ))

    event_bus = EventBus(max_ws=10)

    with patch.dict(os.environ, {}, clear=False):
        from casectl.daemon.server import create_app

        app = create_app(
            plugin_host=plugin_host,
            config_manager=config_manager,
            event_bus=event_bus,
            host="127.0.0.1",
        )

    client = TestClient(app, raise_server_exceptions=False)
    return client, plugin_host, config_manager, event_bus


# ---------------------------------------------------------------------------
# Tests: WebSocket set_fan_mode command
# ---------------------------------------------------------------------------


def test_ws_set_fan_mode_by_int() -> None:
    """WebSocket set_fan_mode with integer mode updates config."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": 2}))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["status"] == "ok"
    assert resp["data"]["mode"] == "manual"
    config_manager.update.assert_called_with("fan", {"mode": 2})


def test_ws_set_fan_mode_by_name() -> None:
    """WebSocket set_fan_mode with string name updates config."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": "follow-temp"}))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["mode"] == "follow_temp"
    config_manager.update.assert_called_with("fan", {"mode": 0})


def test_ws_set_fan_mode_off() -> None:
    """WebSocket set_fan_mode 'off' maps to mode 4."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": "off"}))
        resp = _recv_command_result(ws)

    assert resp["data"]["mode"] == "off"
    config_manager.update.assert_called_with("fan", {"mode": 4})


def test_ws_set_fan_mode_invalid_name() -> None:
    """WebSocket set_fan_mode with invalid name returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": "turbo"}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "Unknown fan mode" in resp["data"]["detail"]


def test_ws_set_fan_mode_missing_param() -> None:
    """WebSocket set_fan_mode without mode parameter returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode"}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "Missing" in resp["data"]["detail"]


# ---------------------------------------------------------------------------
# Tests: WebSocket set_led_mode command
# ---------------------------------------------------------------------------


def test_ws_set_led_mode_by_int() -> None:
    """WebSocket set_led_mode with integer mode updates config."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": 0}))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["mode"] == "rainbow"
    config_manager.update.assert_called_with("led", {"mode": 0})


def test_ws_set_led_mode_by_name() -> None:
    """WebSocket set_led_mode with string name updates config."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": "breathing"}))
        resp = _recv_command_result(ws)

    assert resp["data"]["mode"] == "breathing"
    config_manager.update.assert_called_with("led", {"mode": 1})


def test_ws_set_led_mode_off() -> None:
    """WebSocket set_led_mode 'off' maps to mode 5."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": "off"}))
        resp = _recv_command_result(ws)

    assert resp["data"]["mode"] == "off"
    config_manager.update.assert_called_with("led", {"mode": 5})


def test_ws_set_led_mode_follow_temp() -> None:
    """WebSocket set_led_mode 'follow-temp' works with hyphen."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": "follow-temp"}))
        resp = _recv_command_result(ws)

    assert resp["data"]["mode"] == "follow_temp"
    config_manager.update.assert_called_with("led", {"mode": 2})


def test_ws_set_led_mode_invalid() -> None:
    """WebSocket set_led_mode with invalid name returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": "strobe"}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "Unknown LED mode" in resp["data"]["detail"]


# ---------------------------------------------------------------------------
# Tests: WebSocket set_led_color command
# ---------------------------------------------------------------------------


def test_ws_set_led_color() -> None:
    """WebSocket set_led_color updates config with RGB values and switches to manual."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_led_color",
            "red": 255, "green": 128, "blue": 0,
        }))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["color"] == {"red": 255, "green": 128, "blue": 0}
    config_manager.update.assert_called_with("led", {
        "mode": 3,  # LedMode.MANUAL
        "red_value": 255,
        "green_value": 128,
        "blue_value": 0,
    })


def test_ws_set_led_color_invalid_range() -> None:
    """WebSocket set_led_color with out-of-range value returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_led_color",
            "red": 300, "green": 0, "blue": 0,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "red" in resp["data"]["detail"]


# ---------------------------------------------------------------------------
# Tests: WebSocket set_fan_speed command
# ---------------------------------------------------------------------------


def test_ws_set_fan_speed() -> None:
    """WebSocket set_fan_speed converts 0-100 to 0-255 and switches to manual."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [50]}))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    # 50% of 255 = 127
    assert resp["data"]["duty_hw"] == [127, 127, 127]  # padded to 3 channels
    config_manager.update.assert_called_with("fan", {
        "mode": 2,  # FanMode.MANUAL
        "manual_duty": [127, 127, 127],
    })


def test_ws_set_fan_speed_three_channels() -> None:
    """WebSocket set_fan_speed with 3 channels doesn't pad."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [100, 50, 0]}))
        resp = _recv_command_result(ws)

    assert resp["data"]["duty_hw"] == [255, 127, 0]


def test_ws_set_fan_speed_invalid() -> None:
    """WebSocket set_fan_speed with invalid duty returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [150]}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "0-100" in resp["data"]["detail"]


# ---------------------------------------------------------------------------
# Tests: WebSocket error handling
# ---------------------------------------------------------------------------


def test_ws_invalid_json() -> None:
    """WebSocket with invalid JSON returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text("not json at all")
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "Invalid JSON" in resp["data"]["detail"]


def test_ws_unknown_command() -> None:
    """WebSocket with unknown command returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "self_destruct"}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "Unknown command" in resp["data"]["detail"]


def test_ws_keepalive_ignored() -> None:
    """Messages without 'command' key are treated as keepalive (no response)."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"ping": True}))
        # Send a real command after to verify the connection is still alive
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": 0}))
        resp = _recv_command_result(ws)
        assert resp["type"] == "command_result"


def test_ws_empty_object_ignored() -> None:
    """Empty JSON object is treated as keepalive."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({}))
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": 0}))
        resp = _recv_command_result(ws)
        assert resp["type"] == "command_result"


# ---------------------------------------------------------------------------
# Tests: Fan dropdown partial rendering
# ---------------------------------------------------------------------------


def _make_web_test_client(
    fan_mode: str = "follow_temp",
    led_mode: str = "rainbow",
) -> TestClient:
    """Build a FastAPI app with web router for partial rendering tests."""
    plugin_host = MagicMock()
    config_manager = MagicMock()

    monitor_plugin = MagicMock()
    monitor_plugin.get_status.return_value = {
        "metrics": {
            "cpu_temp": 45.0, "cpu_percent": 20.0,
            "memory_percent": 40.0, "disk_percent": 30.0,
            "ip_address": "10.0.0.1", "case_temp": 28.0,
            "motor_speed": [800, 900, 850],
        },
    }

    fan_plugin = MagicMock()
    fan_plugin.get_status.return_value = {
        "mode": fan_mode,
        "duty": [128, 128, 128],
        "degraded": False,
    }

    led_plugin = MagicMock()
    led_plugin.get_status.return_value = {
        "mode": led_mode,
        "color": {"red": 255, "green": 0, "blue": 128},
        "degraded": False,
    }

    oled_plugin = MagicMock()
    oled_plugin.get_status.return_value = {
        "current_screen": 0,
        "screen_names": ["clock", "metrics", "temperature", "fan_duty"],
        "screens_enabled": [True, True, True, True],
        "rotation": 180, "degraded": False,
    }

    def get_plugin(name: str) -> MagicMock | None:
        return {
            "system-monitor": monitor_plugin,
            "fan-control": fan_plugin,
            "led-control": led_plugin,
            "oled-display": oled_plugin,
        }.get(name)

    plugin_host.get_plugin.side_effect = get_plugin
    plugin_host.get_all_statuses.return_value = {
        "fan-control": PluginStatus.HEALTHY,
        "led-control": PluginStatus.HEALTHY,
    }

    router = create_web_router(plugin_host, config_manager)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_fan_partial_renders_dropdown() -> None:
    """Fan partial renders a <select> element for mode control."""
    client = _make_web_test_client(fan_mode="follow_temp")
    resp = client.get("/w/fan")
    assert resp.status_code == 200
    assert "<select" in resp.text
    assert 'id="fan-mode-select"' in resp.text


def test_fan_partial_selects_current_mode() -> None:
    """Fan partial marks the current mode as selected in the dropdown."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert resp.status_code == 200
    # "Manual" option should have 'selected'
    assert 'value="2" selected' in resp.text


def test_fan_partial_follow_temp_selected() -> None:
    """Fan partial marks follow_temp as selected when active."""
    client = _make_web_test_client(fan_mode="follow_temp")
    resp = client.get("/w/fan")
    assert 'value="0" selected' in resp.text


def test_fan_partial_off_selected() -> None:
    """Fan partial marks off as selected when active."""
    client = _make_web_test_client(fan_mode="off")
    resp = client.get("/w/fan")
    assert 'value="4" selected' in resp.text


def test_fan_partial_manual_shows_slider() -> None:
    """Fan partial shows duty slider in manual mode."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert 'id="fan-duty-slider"' in resp.text


def test_fan_partial_auto_hides_slider() -> None:
    """Fan partial hides duty slider when not in manual mode."""
    client = _make_web_test_client(fan_mode="follow_temp")
    resp = client.get("/w/fan")
    assert 'id="fan-duty-slider"' not in resp.text


def test_fan_partial_has_ws_data_attr() -> None:
    """Fan dropdown has data-ws-command attribute for WebSocket binding."""
    client = _make_web_test_client()
    resp = client.get("/w/fan")
    assert 'data-ws-command="set_fan_mode"' in resp.text


# ---------------------------------------------------------------------------
# Tests: LED dropdown partial rendering
# ---------------------------------------------------------------------------


def test_led_partial_renders_dropdown() -> None:
    """LED partial renders a <select> element for mode control."""
    client = _make_web_test_client(led_mode="rainbow")
    resp = client.get("/w/led")
    assert resp.status_code == 200
    assert "<select" in resp.text
    assert 'id="led-mode-select"' in resp.text


def test_led_partial_selects_current_mode() -> None:
    """LED partial marks the current mode as selected in the dropdown."""
    client = _make_web_test_client(led_mode="breathing")
    resp = client.get("/w/led")
    assert 'value="1" selected' in resp.text


def test_led_partial_rainbow_selected() -> None:
    """LED partial marks rainbow as selected when active."""
    client = _make_web_test_client(led_mode="rainbow")
    resp = client.get("/w/led")
    assert 'value="0" selected' in resp.text


def test_led_partial_off_selected() -> None:
    """LED partial marks off as selected when active."""
    client = _make_web_test_client(led_mode="off")
    resp = client.get("/w/led")
    assert 'value="5" selected' in resp.text


def test_led_partial_manual_shows_color_picker() -> None:
    """LED partial shows colour picker in manual mode."""
    client = _make_web_test_client(led_mode="manual")
    resp = client.get("/w/led")
    assert 'id="led-color-picker"' in resp.text


def test_led_partial_rainbow_hides_color_picker() -> None:
    """LED partial hides colour picker when not in manual mode."""
    client = _make_web_test_client(led_mode="rainbow")
    resp = client.get("/w/led")
    assert 'id="led-color-picker"' not in resp.text


def test_led_partial_has_ws_data_attr() -> None:
    """LED dropdown has data-ws-command attribute for WebSocket binding."""
    client = _make_web_test_client()
    resp = client.get("/w/led")
    assert 'data-ws-command="set_led_mode"' in resp.text


def test_led_partial_color_picker_has_ws_attr() -> None:
    """LED colour picker has data-ws-command attribute for WebSocket."""
    client = _make_web_test_client(led_mode="manual")
    resp = client.get("/w/led")
    assert 'data-ws-command="set_led_color"' in resp.text


# ---------------------------------------------------------------------------
# Tests: Dashboard renders dropdown-based partials
# ---------------------------------------------------------------------------


def test_dashboard_contains_fan_dropdown() -> None:
    """Full dashboard page includes the fan mode dropdown."""
    client = _make_web_test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="fan-mode-select"' in resp.text


def test_dashboard_contains_led_dropdown() -> None:
    """Full dashboard page includes the LED mode dropdown."""
    client = _make_web_test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="led-mode-select"' in resp.text


def test_dashboard_includes_ws_controls_script() -> None:
    """Dashboard base template includes the ws-controls.js script tag."""
    client = _make_web_test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ws-controls.js" in resp.text


# ---------------------------------------------------------------------------
# Tests: WebSocket config_manager error propagation
# ---------------------------------------------------------------------------


def test_ws_command_config_error_returns_500() -> None:
    """WebSocket command that triggers config_manager error returns error."""
    client, _, config_manager, _ = _make_ws_test_app()
    config_manager.update = AsyncMock(side_effect=RuntimeError("disk full"))

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": 0}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "Internal server error" in resp["data"]["detail"]


# ---------------------------------------------------------------------------
# Tests: Mode resolution edge cases
# ---------------------------------------------------------------------------


def test_ws_fan_mode_follow_rpi_by_name() -> None:
    """WebSocket set_fan_mode 'follow-rpi' resolves correctly."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": "follow_rpi"}))
        resp = _recv_command_result(ws)

    assert resp["data"]["mode"] == "follow_rpi"
    config_manager.update.assert_called_with("fan", {"mode": 1})


def test_ws_fan_mode_invalid_int() -> None:
    """WebSocket set_fan_mode with out-of-range integer returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": 99}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"


def test_ws_led_mode_custom_by_int() -> None:
    """WebSocket set_led_mode with mode 4 (custom) works."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": 4}))
        resp = _recv_command_result(ws)

    assert resp["data"]["mode"] == "custom"
    config_manager.update.assert_called_with("led", {"mode": 4})


# ---------------------------------------------------------------------------
# Tests: WebSocket commands emit EventBus events for real-time broadcast
# ---------------------------------------------------------------------------


def test_ws_set_fan_mode_emits_event() -> None:
    """set_fan_mode emits fan.mode_changed event on the EventBus."""
    client, _, _, event_bus = _make_ws_test_app()
    events_received: list[dict] = []

    async def capture_event(data: Any) -> None:
        events_received.append(data)

    event_bus.subscribe("fan.mode_changed", capture_event)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": 0}))
        _recv_command_result(ws)

    assert len(events_received) == 1
    assert events_received[0]["mode"] == "follow_temp"
    assert events_received[0]["mode_value"] == 0
    assert events_received[0]["source"] == "websocket"
    assert "ts" in events_received[0]


def test_ws_set_led_mode_emits_event() -> None:
    """set_led_mode emits led.mode_changed event on the EventBus."""
    client, _, _, event_bus = _make_ws_test_app()
    events_received: list[dict] = []

    async def capture_event(data: Any) -> None:
        events_received.append(data)

    event_bus.subscribe("led.mode_changed", capture_event)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_led_mode", "mode": "rainbow"}))
        _recv_command_result(ws)

    assert len(events_received) == 1
    assert events_received[0]["mode"] == "rainbow"
    assert events_received[0]["mode_value"] == 0
    assert events_received[0]["source"] == "websocket"


def test_ws_set_led_color_emits_event() -> None:
    """set_led_color emits led.color_changed event on the EventBus."""
    client, _, _, event_bus = _make_ws_test_app()
    events_received: list[dict] = []

    async def capture_event(data: Any) -> None:
        events_received.append(data)

    event_bus.subscribe("led.color_changed", capture_event)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_led_color",
            "red": 100, "green": 200, "blue": 50,
        }))
        _recv_command_result(ws)

    assert len(events_received) == 1
    assert events_received[0]["color"] == {"red": 100, "green": 200, "blue": 50}
    assert events_received[0]["source"] == "websocket"


def test_ws_set_fan_speed_emits_event() -> None:
    """set_fan_speed emits fan.speed_changed event on the EventBus."""
    client, _, _, event_bus = _make_ws_test_app()
    events_received: list[dict] = []

    async def capture_event(data: Any) -> None:
        events_received.append(data)

    event_bus.subscribe("fan.speed_changed", capture_event)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [50]}))
        _recv_command_result(ws)

    assert len(events_received) == 1
    assert "duty_hw" in events_received[0]
    assert events_received[0]["source"] == "websocket"


def test_ws_failed_command_does_not_emit_event() -> None:
    """Failed commands should not emit events (error raised before emit)."""
    client, _, _, event_bus = _make_ws_test_app()
    events_received: list[dict] = []

    async def capture_event(data: Any) -> None:
        events_received.append(data)

    event_bus.subscribe("fan.mode_changed", capture_event)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": 99}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert len(events_received) == 0  # No event emitted for invalid command


# ---------------------------------------------------------------------------
# Tests: Multiple sequential commands and CLI parity
# ---------------------------------------------------------------------------


def test_ws_multiple_commands_sequential() -> None:
    """Multiple commands can be sent sequentially on a single connection."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_mode", "mode": 2}))
        r1 = _recv_command_result(ws)

        ws.send_text(json.dumps({"command": "set_led_mode", "mode": 0}))
        r2 = _recv_command_result(ws)

    assert r1["data"]["mode"] == "manual"
    assert r2["data"]["mode"] == "rainbow"
    assert config_manager.update.call_count == 2


def test_ws_all_fan_modes_by_int() -> None:
    """All 5 fan modes can be set by integer value via WebSocket."""
    client, _, config_manager, _ = _make_ws_test_app()
    expected = ["follow_temp", "follow_rpi", "manual", "custom", "off"]

    for mode_val in range(5):
        config_manager.update.reset_mock()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"command": "set_fan_mode", "mode": mode_val}))
            resp = _recv_command_result(ws)
        assert resp["type"] == "command_result"
        assert resp["data"]["mode"] == expected[mode_val]
        config_manager.update.assert_called_once_with("fan", {"mode": mode_val})


def test_ws_all_led_modes_by_int() -> None:
    """All 6 LED modes can be set by integer value via WebSocket."""
    client, _, config_manager, _ = _make_ws_test_app()
    expected = ["rainbow", "breathing", "follow_temp", "manual", "custom", "off"]

    for mode_val in range(6):
        config_manager.update.reset_mock()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"command": "set_led_mode", "mode": mode_val}))
            resp = _recv_command_result(ws)
        assert resp["type"] == "command_result"
        assert resp["data"]["mode"] == expected[mode_val]
        config_manager.update.assert_called_once_with("led", {"mode": mode_val})


def test_dashboard_includes_realtime_script() -> None:
    """Dashboard base template includes the realtime.js script tag."""
    client = _make_web_test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "realtime.js" in resp.text


# ---------------------------------------------------------------------------
# Tests: Per-channel fan speed slider rendering
# ---------------------------------------------------------------------------


def test_fan_partial_manual_shows_per_channel_sliders() -> None:
    """Fan partial renders per-channel duty sliders in manual mode."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert resp.status_code == 200
    # All 3 channels should have individual sliders
    assert 'id="fan-0-duty-slider"' in resp.text
    assert 'id="fan-1-duty-slider"' in resp.text
    assert 'id="fan-2-duty-slider"' in resp.text


def test_fan_partial_manual_channel_sliders_have_ws_attr() -> None:
    """Per-channel sliders have data-ws-command for WebSocket binding."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert 'data-ws-command="set_fan_speed"' in resp.text


def test_fan_partial_manual_channel_sliders_have_data_attr() -> None:
    """Per-channel sliders have data-fan-channel attribute."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert 'data-fan-channel="0"' in resp.text
    assert 'data-fan-channel="1"' in resp.text
    assert 'data-fan-channel="2"' in resp.text


def test_fan_partial_auto_hides_per_channel_sliders() -> None:
    """Per-channel sliders are hidden when not in manual mode."""
    client = _make_web_test_client(fan_mode="follow_temp")
    resp = client.get("/w/fan")
    assert 'id="fan-0-duty-slider"' not in resp.text
    assert 'id="fan-1-duty-slider"' not in resp.text
    assert 'id="fan-2-duty-slider"' not in resp.text


def test_fan_partial_off_hides_per_channel_sliders() -> None:
    """Per-channel sliders are hidden in off mode."""
    client = _make_web_test_client(fan_mode="off")
    resp = client.get("/w/fan")
    assert 'id="fan-0-duty-slider"' not in resp.text


def test_fan_partial_channel_slider_value_display() -> None:
    """Per-channel sliders show duty value display elements."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert 'id="fan-0-duty-value"' in resp.text
    assert 'id="fan-1-duty-value"' in resp.text
    assert 'id="fan-2-duty-value"' in resp.text


def test_fan_partial_channel_progress_ids() -> None:
    """Fan channels have progress bar IDs for dynamic JS updates."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert 'id="fan-0-progress"' in resp.text
    assert 'id="fan-1-progress"' in resp.text
    assert 'id="fan-2-progress"' in resp.text


def test_fan_partial_channel_duty_display_ids() -> None:
    """Fan channels have duty display IDs for dynamic JS updates."""
    client = _make_web_test_client(fan_mode="follow_temp")
    resp = client.get("/w/fan")
    # Duty display IDs exist in all modes (progress always visible)
    assert 'id="fan-0-duty-display"' in resp.text
    assert 'id="fan-1-duty-display"' in resp.text
    assert 'id="fan-2-duty-display"' in resp.text


def test_fan_partial_unified_and_channel_sliders_coexist() -> None:
    """Both unified and per-channel sliders exist in manual mode."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    # Unified slider
    assert 'id="fan-duty-slider"' in resp.text
    # Per-channel sliders
    assert 'id="fan-0-duty-slider"' in resp.text
    assert 'id="fan-1-duty-slider"' in resp.text
    assert 'id="fan-2-duty-slider"' in resp.text


def test_fan_partial_slider_has_correct_range() -> None:
    """Per-channel sliders have min=0 max=100 step=1."""
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    assert 'min="0"' in resp.text
    assert 'max="100"' in resp.text
    assert 'step="1"' in resp.text


def test_fan_partial_slider_reflects_duty() -> None:
    """Per-channel slider values reflect the current duty from hardware."""
    # Duty [128, 128, 128] = 50% (128/255*100 rounded)
    client = _make_web_test_client(fan_mode="manual")
    resp = client.get("/w/fan")
    # 128/255*100 = ~50.2 → rounds to 50
    assert 'value="50"' in resp.text


# ---------------------------------------------------------------------------
# Tests: WebSocket set_fan_speed per-channel parity with CLI
# ---------------------------------------------------------------------------


def test_ws_set_fan_speed_per_channel() -> None:
    """WebSocket set_fan_speed with per-channel values (CLI parity)."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [75, 50, 25]}))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    # 75% → 191, 50% → 127, 25% → 63
    assert resp["data"]["duty_hw"] == [191, 127, 63]
    config_manager.update.assert_called_with("fan", {
        "mode": 2,  # FanMode.MANUAL
        "manual_duty": [191, 127, 63],
    })


def test_ws_set_fan_speed_zero() -> None:
    """WebSocket set_fan_speed with 0% sets fans to off."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [0]}))
        resp = _recv_command_result(ws)

    assert resp["data"]["duty_hw"] == [0, 0, 0]


def test_ws_set_fan_speed_full() -> None:
    """WebSocket set_fan_speed with 100% sets fans to maximum."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [100]}))
        resp = _recv_command_result(ws)

    assert resp["data"]["duty_hw"] == [255, 255, 255]


def test_ws_set_fan_speed_empty_list() -> None:
    """WebSocket set_fan_speed with empty list returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": []}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"


def test_ws_set_fan_speed_too_many_channels() -> None:
    """WebSocket set_fan_speed with 4+ channels returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [50, 50, 50, 50]}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"


def test_ws_set_fan_speed_negative_duty() -> None:
    """WebSocket set_fan_speed with negative duty returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [-10]}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"


def test_ws_set_fan_speed_switches_to_manual() -> None:
    """Setting fan speed via WebSocket automatically switches mode to manual."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [50]}))
        _recv_command_result(ws)

    # Verify mode=2 (MANUAL) is included in the config update
    call_args = config_manager.update.call_args
    assert call_args[0][1]["mode"] == 2  # FanMode.MANUAL


def test_ws_set_fan_speed_two_channels_padded() -> None:
    """WebSocket set_fan_speed with 2 channels pads the third."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_fan_speed", "duty": [80, 40]}))
        resp = _recv_command_result(ws)

    # 80% → 204, 40% → 102, padded with last value → 102
    assert resp["data"]["duty_hw"] == [204, 102, 102]


# ---------------------------------------------------------------------------
# Tests: WebSocket set_oled_screen command
# ---------------------------------------------------------------------------


def _make_ws_test_app_with_oled_config() -> tuple[TestClient, MagicMock, MagicMock, EventBus]:
    """Create a WebSocket test app with OLED config pre-populated."""
    client, plugin_host, config_manager, event_bus = _make_ws_test_app()

    # Set up config_manager.get("oled") to return realistic OLED config.
    _oled_screens = [
        {"enabled": True, "display_time": 5.0, "date_format": 0, "time_format": 0, "interchange": 0},
        {"enabled": True, "display_time": 5.0, "date_format": 0, "time_format": 0, "interchange": 0},
        {"enabled": True, "display_time": 5.0, "date_format": 0, "time_format": 0, "interchange": 0},
        {"enabled": False, "display_time": 5.0, "date_format": 0, "time_format": 0, "interchange": 0},
    ]

    async def _get_oled(section: str) -> dict[str, Any]:
        if section == "oled":
            return {"screens": _oled_screens, "rotation": 180}
        return {}

    config_manager.get = AsyncMock(side_effect=_get_oled)
    return client, plugin_host, config_manager, event_bus


def test_ws_set_oled_screen_disable() -> None:
    """WebSocket set_oled_screen disables a screen by index."""
    client, _, config_manager, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_screen", "index": 1, "enabled": False,
        }))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["status"] == "ok"
    assert resp["data"]["index"] == 1
    assert resp["data"]["enabled"] is False
    config_manager.update.assert_called_once()
    call_args = config_manager.update.call_args
    assert call_args[0][0] == "oled"
    assert call_args[0][1]["screens"][1]["enabled"] is False


def test_ws_set_oled_screen_enable() -> None:
    """WebSocket set_oled_screen enables a previously disabled screen."""
    client, _, config_manager, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_screen", "index": 3, "enabled": True,
        }))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["enabled"] is True
    call_args = config_manager.update.call_args
    assert call_args[0][1]["screens"][3]["enabled"] is True


def test_ws_set_oled_screen_invalid_index() -> None:
    """WebSocket set_oled_screen with invalid index returns error."""
    client, _, _, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_screen", "index": 5, "enabled": True,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "index" in resp["data"]["detail"].lower()


def test_ws_set_oled_screen_missing_enabled() -> None:
    """WebSocket set_oled_screen without enabled parameter returns error."""
    client, _, _, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_screen", "index": 0,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "enabled" in resp["data"]["detail"].lower()


def test_ws_set_oled_screen_missing_index() -> None:
    """WebSocket set_oled_screen without index returns error."""
    client, _, _, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_screen", "enabled": True,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "index" in resp["data"]["detail"].lower()


# ---------------------------------------------------------------------------
# Tests: WebSocket set_oled_rotation command
# ---------------------------------------------------------------------------


def test_ws_set_oled_rotation_0() -> None:
    """WebSocket set_oled_rotation sets rotation to 0."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_rotation", "rotation": 0}))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["status"] == "ok"
    assert resp["data"]["rotation"] == 0
    config_manager.update.assert_called_with("oled", {"rotation": 0})


def test_ws_set_oled_rotation_180() -> None:
    """WebSocket set_oled_rotation sets rotation to 180."""
    client, _, config_manager, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_rotation", "rotation": 180}))
        resp = _recv_command_result(ws)

    assert resp["data"]["rotation"] == 180
    config_manager.update.assert_called_with("oled", {"rotation": 180})


def test_ws_set_oled_rotation_invalid() -> None:
    """WebSocket set_oled_rotation with invalid value returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_rotation", "rotation": 90}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "rotation" in resp["data"]["detail"].lower()


def test_ws_set_oled_rotation_missing() -> None:
    """WebSocket set_oled_rotation without rotation param returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_rotation"}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"


# ---------------------------------------------------------------------------
# Tests: WebSocket set_oled_power command
# ---------------------------------------------------------------------------


def test_ws_set_oled_power_off() -> None:
    """WebSocket set_oled_power disables all screens."""
    client, _, config_manager, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_power", "enabled": False}))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["status"] == "ok"
    assert resp["data"]["enabled"] is False
    call_args = config_manager.update.call_args
    screens = call_args[0][1]["screens"]
    assert all(s["enabled"] is False for s in screens)


def test_ws_set_oled_power_on() -> None:
    """WebSocket set_oled_power enables all screens."""
    client, _, config_manager, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_power", "enabled": True}))
        resp = _recv_command_result(ws)

    assert resp["data"]["enabled"] is True
    call_args = config_manager.update.call_args
    screens = call_args[0][1]["screens"]
    assert all(s["enabled"] is True for s in screens)


def test_ws_set_oled_power_missing_enabled() -> None:
    """WebSocket set_oled_power without enabled returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_power"}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "enabled" in resp["data"]["detail"].lower()


def test_ws_set_oled_power_invalid_type() -> None:
    """WebSocket set_oled_power with non-bool enabled returns error."""
    client, _, _, _ = _make_ws_test_app()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"command": "set_oled_power", "enabled": "yes"}))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"


# ---------------------------------------------------------------------------
# Tests: WebSocket set_oled_content command
# ---------------------------------------------------------------------------


def test_ws_set_oled_content_display_time() -> None:
    """WebSocket set_oled_content updates display_time for a screen."""
    client, _, config_manager, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_content",
            "screen_index": 0,
            "display_time": 10.0,
        }))
        resp = _recv_command_result(ws)

    assert resp["type"] == "command_result"
    assert resp["data"]["status"] == "ok"
    assert resp["data"]["screen_index"] == 0
    assert resp["data"]["settings"]["display_time"] == 10.0
    call_args = config_manager.update.call_args
    assert call_args[0][1]["screens"][0]["display_time"] == 10.0


def test_ws_set_oled_content_time_format() -> None:
    """WebSocket set_oled_content updates time_format for a screen."""
    client, _, config_manager, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_content",
            "screen_index": 0,
            "time_format": 1,
        }))
        resp = _recv_command_result(ws)

    assert resp["data"]["settings"]["time_format"] == 1


def test_ws_set_oled_content_multiple_fields() -> None:
    """WebSocket set_oled_content can update multiple settings at once."""
    client, _, config_manager, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_content",
            "screen_index": 2,
            "display_time": 8.0,
            "interchange": 1,
        }))
        resp = _recv_command_result(ws)

    assert resp["data"]["settings"]["display_time"] == 8.0
    assert resp["data"]["settings"]["interchange"] == 1


def test_ws_set_oled_content_invalid_screen_index() -> None:
    """WebSocket set_oled_content with invalid screen_index returns error."""
    client, _, _, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_content",
            "screen_index": 5,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "screen_index" in resp["data"]["detail"].lower()


def test_ws_set_oled_content_missing_screen_index() -> None:
    """WebSocket set_oled_content without screen_index returns error."""
    client, _, _, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_content",
            "display_time": 5.0,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "screen_index" in resp["data"]["detail"].lower()


def test_ws_set_oled_content_invalid_display_time() -> None:
    """WebSocket set_oled_content with negative display_time returns error."""
    client, _, _, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_content",
            "screen_index": 0,
            "display_time": -1,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "display_time" in resp["data"]["detail"].lower()


def test_ws_set_oled_content_invalid_time_format() -> None:
    """WebSocket set_oled_content with invalid time_format returns error."""
    client, _, _, _ = _make_ws_test_app_with_oled_config()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "command": "set_oled_content",
            "screen_index": 0,
            "time_format": 3,
        }))
        resp = json.loads(ws.receive_text())

    assert resp["type"] == "error"
    assert "time_format" in resp["data"]["detail"].lower()
