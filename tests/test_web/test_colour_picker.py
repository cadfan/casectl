"""Tests for the LED colour picker component and WebSocket colour handling.

Exercises:
- WebSocket set_led_color with RGB, hex, and named colour inputs (CLI parity)
- LED partial rendering with colour picker controls
- Named colour resolution
- Hex colour validation
- RGB value validation
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


def _make_mock_plugin(status_data: dict[str, Any]) -> MagicMock:
    """Create a mock plugin with a get_status() that returns *status_data*."""
    plugin = MagicMock()
    plugin.get_status.return_value = status_data
    return plugin


def _make_ws_test_app() -> tuple[TestClient, MagicMock, MagicMock, EventBus]:
    """Build a FastAPI app with WebSocket handler for colour testing.

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
    config_manager.update = AsyncMock()

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


def _recv_command_result(ws, *, max_messages: int = 10) -> dict:
    """Read WebSocket messages until a command_result or error is found.

    The WebSocket handler emits an event (broadcast to all WS subscribers
    including the sender) *before* sending the command_result.  This helper
    skips broadcast frames to find the actual response.
    """
    for _ in range(max_messages):
        raw = ws.receive_text()
        msg = json.loads(raw)
        if msg.get("type") in ("command_result", "error"):
            return msg
    raise AssertionError(f"No command_result/error in {max_messages} messages")


def _make_led_partial_client(
    mode: str = "manual",
    color: dict[str, int] | None = None,
) -> TestClient:
    """Build a TestClient for the web dashboard with LED data."""
    if color is None:
        color = {"red": 128, "green": 64, "blue": 200}

    plugin_host = MagicMock()
    config_manager = MagicMock()

    led_plugin = _make_mock_plugin({
        "mode": mode,
        "color": color,
        "degraded": False,
    })

    def get_plugin(name: str) -> MagicMock | None:
        if name == "led-control":
            return led_plugin
        return None

    plugin_host.get_plugin.side_effect = get_plugin
    plugin_host.get_all_statuses.return_value = {
        "led-control": PluginStatus.HEALTHY,
    }

    router = create_web_router(plugin_host, config_manager)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Tests: WebSocket set_led_color with RGB values
# ---------------------------------------------------------------------------


class TestWsColourRgb:
    """WebSocket set_led_color with explicit RGB values."""

    def test_rgb_values_accepted(self) -> None:
        """set_led_color with red/green/blue int values succeeds."""
        client, _, config_manager, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": 255, "green": 128, "blue": 0,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["status"] == "ok"
            assert resp["data"]["color"] == {"red": 255, "green": 128, "blue": 0}

    def test_rgb_zero_values(self) -> None:
        """set_led_color with all zeros (off) succeeds."""
        client, _, config_manager, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": 0, "green": 0, "blue": 0,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 0, "green": 0, "blue": 0}

    def test_rgb_max_values(self) -> None:
        """set_led_color with all 255s (white) succeeds."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": 255, "green": 255, "blue": 255,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 255, "green": 255, "blue": 255}

    def test_rgb_out_of_range_rejected(self) -> None:
        """set_led_color with a value >255 returns error."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": 300, "green": 0, "blue": 0,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "error"
            assert "red" in resp["data"]["detail"].lower()

    def test_rgb_negative_rejected(self) -> None:
        """set_led_color with a negative value returns error."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": -1, "green": 0, "blue": 0,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "error"

    def test_rgb_non_integer_rejected(self) -> None:
        """set_led_color with a string value returns error."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": "ff", "green": 0, "blue": 0,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "error"

    def test_rgb_updates_config_to_manual_mode(self) -> None:
        """set_led_color switches config to MANUAL mode."""
        client, _, config_manager, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": 100, "green": 200, "blue": 50,
            }))
            _recv_command_result(ws)

        # Verify config_manager.update was called with manual mode
        config_manager.update.assert_called()
        call_args = config_manager.update.call_args
        assert call_args[0][0] == "led"
        update_dict = call_args[0][1]
        assert update_dict["red_value"] == 100
        assert update_dict["green_value"] == 200
        assert update_dict["blue_value"] == 50
        assert "mode" in update_dict  # manual mode


# ---------------------------------------------------------------------------
# Tests: WebSocket set_led_color with named colours (CLI parity)
# ---------------------------------------------------------------------------


class TestWsColourNamed:
    """WebSocket set_led_color with color_name -- matches `casectl led color <name>`."""

    def test_named_red(self) -> None:
        """color_name='red' resolves to (255, 0, 0)."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "color_name": "red",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 255, "green": 0, "blue": 0}

    def test_named_arctic_steel(self) -> None:
        """color_name='arctic-steel' resolves to (138, 170, 196)."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "color_name": "arctic-steel",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 138, "green": 170, "blue": 196}

    def test_named_case_insensitive(self) -> None:
        """color_name is case-insensitive."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "color_name": "CYAN",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 0, "green": 255, "blue": 255}

    def test_named_unknown_rejected(self) -> None:
        """Unknown colour name returns error."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "color_name": "chartreuse",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "error"
            assert "chartreuse" in resp["data"]["detail"].lower()

    def test_named_takes_priority_over_rgb(self) -> None:
        """When both color_name and RGB are provided, color_name wins."""
        client, _, config_manager, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "color_name": "blue",
                "red": 255, "green": 255, "blue": 255,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            # Named colour wins: blue=(0,0,255) not white=(255,255,255)
            assert resp["data"]["color"] == {"red": 0, "green": 0, "blue": 255}

    @pytest.mark.parametrize("name", [
        "red", "green", "blue", "white", "yellow", "cyan", "magenta",
        "orange", "pink", "purple", "teal", "coral", "gold", "lime",
        "navy", "arctic-steel",
    ])
    def test_all_16_named_colours(self, name: str) -> None:
        """All 16 CLI-supported named colours are accepted."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "color_name": name,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            color = resp["data"]["color"]
            assert 0 <= color["red"] <= 255
            assert 0 <= color["green"] <= 255
            assert 0 <= color["blue"] <= 255


# ---------------------------------------------------------------------------
# Tests: WebSocket set_led_color with hex code (CLI parity)
# ---------------------------------------------------------------------------


class TestWsColourHex:
    """WebSocket set_led_color with hex -- matches `casectl led color #FF0080`."""

    def test_hex_valid(self) -> None:
        """hex='#FF0080' resolves correctly."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "hex": "#FF0080",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 255, "green": 0, "blue": 128}

    def test_hex_lowercase(self) -> None:
        """hex='#ff0080' (lowercase) resolves correctly."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "hex": "#ff0080",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 255, "green": 0, "blue": 128}

    def test_hex_without_hash(self) -> None:
        """hex='FF0080' (no #) resolves correctly."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "hex": "FF0080",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 255, "green": 0, "blue": 128}

    def test_hex_invalid_length(self) -> None:
        """hex='#FFF' (3 digits) is rejected."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "hex": "#FFF",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "error"
            assert "6 digits" in resp["data"]["detail"]

    def test_hex_invalid_chars(self) -> None:
        """hex='#GGGGGG' is rejected."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "hex": "#GGGGGG",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "error"

    def test_hex_black(self) -> None:
        """hex='#000000' resolves to black."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "hex": "#000000",
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 0, "green": 0, "blue": 0}

    def test_hex_takes_priority_over_rgb(self) -> None:
        """When both hex and RGB are provided, hex wins."""
        client, _, _, _ = _make_ws_test_app()
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "hex": "#FF0000",
                "red": 0, "green": 255, "blue": 0,
            }))
            resp = _recv_command_result(ws)
            assert resp["type"] == "command_result"
            assert resp["data"]["color"] == {"red": 255, "green": 0, "blue": 0}


# ---------------------------------------------------------------------------
# Tests: LED partial template renders colour picker
# ---------------------------------------------------------------------------


class TestLedPartialColourPicker:
    """Verify the LED partial renders the colour picker component."""

    def test_manual_mode_shows_colour_picker(self) -> None:
        """In manual mode, the partial renders the colour-picker-component."""
        client = _make_led_partial_client(mode="manual", color={"red": 128, "green": 64, "blue": 200})
        resp = client.get("/w/led")
        assert resp.status_code == 200
        assert "colour-picker-component" in resp.text

    def test_custom_mode_shows_colour_picker(self) -> None:
        """In custom mode, the partial also renders the colour picker."""
        client = _make_led_partial_client(mode="custom", color={"red": 0, "green": 255, "blue": 0})
        resp = client.get("/w/led")
        assert resp.status_code == 200
        assert "colour-picker-component" in resp.text

    def test_rainbow_mode_hides_colour_picker(self) -> None:
        """In rainbow mode, the colour picker is not rendered."""
        client = _make_led_partial_client(mode="rainbow")
        resp = client.get("/w/led")
        assert resp.status_code == 200
        assert "colour-picker-component" not in resp.text

    def test_off_mode_hides_colour_picker(self) -> None:
        """In off mode, the colour picker is not rendered."""
        client = _make_led_partial_client(mode="off")
        resp = client.get("/w/led")
        assert resp.status_code == 200
        assert "colour-picker-component" not in resp.text

    def test_colour_picker_shows_hex_input(self) -> None:
        """The colour picker includes a hex text input."""
        client = _make_led_partial_client(mode="manual", color={"red": 255, "green": 0, "blue": 128})
        resp = client.get("/w/led")
        assert "led-hex-input" in resp.text
        assert "#FF0080" in resp.text

    def test_colour_picker_shows_rgb_sliders(self) -> None:
        """The colour picker includes R, G, B range sliders."""
        client = _make_led_partial_client(mode="manual", color={"red": 100, "green": 200, "blue": 50})
        resp = client.get("/w/led")
        assert "led-slider-r" in resp.text
        assert "led-slider-g" in resp.text
        assert "led-slider-b" in resp.text
        assert 'value="100"' in resp.text
        assert 'value="200"' in resp.text
        assert 'value="50"' in resp.text

    def test_colour_picker_shows_named_presets(self) -> None:
        """The colour picker includes named colour preset swatches."""
        client = _make_led_partial_client(mode="manual")
        resp = client.get("/w/led")
        assert "preset-swatch" in resp.text
        assert 'data-color-name="red"' in resp.text
        assert 'data-color-name="arctic-steel"' in resp.text

    def test_colour_picker_shows_native_picker(self) -> None:
        """The colour picker includes the native <input type=color>."""
        client = _make_led_partial_client(mode="manual")
        resp = client.get("/w/led")
        assert 'type="color"' in resp.text
        assert "led-color-picker" in resp.text

    def test_colour_picker_hex_value_correct(self) -> None:
        """The hex input and native picker show the correct current colour."""
        client = _make_led_partial_client(
            mode="manual",
            color={"red": 0, "green": 0, "blue": 255},
        )
        resp = client.get("/w/led")
        assert "#0000FF" in resp.text

    def test_colour_picker_data_attributes(self) -> None:
        """The component root has data-red/green/blue attributes."""
        client = _make_led_partial_client(
            mode="manual",
            color={"red": 10, "green": 20, "blue": 30},
        )
        resp = client.get("/w/led")
        assert 'data-red="10"' in resp.text
        assert 'data-green="20"' in resp.text
        assert 'data-blue="30"' in resp.text

    def test_colour_picker_all_16_presets_present(self) -> None:
        """All 16 named colour presets are rendered."""
        client = _make_led_partial_client(mode="manual")
        resp = client.get("/w/led")
        expected = [
            "red", "green", "blue", "white", "yellow", "cyan", "magenta",
            "orange", "pink", "purple", "teal", "coral", "gold", "lime",
            "navy", "arctic-steel",
        ]
        for name in expected:
            assert f'data-color-name="{name}"' in resp.text, f"Missing preset: {name}"


# ---------------------------------------------------------------------------
# Tests: Event emission on colour change
# ---------------------------------------------------------------------------


class TestWsColourEventEmission:
    """Verify that set_led_color emits led.color_changed on the EventBus."""

    def test_colour_change_emits_event(self) -> None:
        """set_led_color emits led.color_changed event."""
        client, _, _, event_bus = _make_ws_test_app()
        received_events: list[dict] = []

        async def _capture(data: Any) -> None:
            received_events.append(data)

        event_bus.subscribe("led.color_changed", _capture)

        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "red": 42, "green": 84, "blue": 168,
            }))
            _recv_command_result(ws)

        assert len(received_events) == 1
        assert received_events[0]["color"] == {"red": 42, "green": 84, "blue": 168}
        assert received_events[0]["source"] == "websocket"

    def test_named_colour_emits_event_with_resolved_rgb(self) -> None:
        """Named colour set_led_color emits event with resolved RGB values."""
        client, _, _, event_bus = _make_ws_test_app()
        received_events: list[dict] = []

        async def _capture(data: Any) -> None:
            received_events.append(data)

        event_bus.subscribe("led.color_changed", _capture)

        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({
                "command": "set_led_color",
                "color_name": "coral",
            }))
            _recv_command_result(ws)

        assert len(received_events) == 1
        assert received_events[0]["color"] == {"red": 255, "green": 127, "blue": 80}
