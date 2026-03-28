"""Tests for the MQTT device state publisher and command subscriber.

All tests mock the MQTT connection manager so no real broker is needed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.daemon.event_bus import EventBus
from casectl.plugins.mqtt.client import BrokerSettings, MqttConnectionManager
from casectl.plugins.mqtt.state import (
    COMMAND_ENTITIES,
    MAX_CURVE_POINTS,
    DeviceStateManager,
    _FAN_MODE_INTS,
    _FAN_MODE_NAMES,
    _LED_MODE_INTS,
    _LED_MODE_NAMES,
    _parse_color,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def broker_settings() -> BrokerSettings:
    """Return BrokerSettings with test defaults."""
    return BrokerSettings(
        topic_prefix="casectl",
        qos=1,
        retain=True,
    )


@pytest.fixture()
def mock_mqtt(broker_settings: BrokerSettings) -> MqttConnectionManager:
    """Return a mock MqttConnectionManager that appears connected."""
    mgr = MagicMock(spec=MqttConnectionManager)
    mgr.settings = broker_settings
    mgr.is_connected = True
    mgr.publish = AsyncMock()
    mgr.subscribe = AsyncMock()
    mgr.unsubscribe = AsyncMock()
    return mgr


@pytest.fixture()
def mock_config_manager() -> MagicMock:
    """Return a mock ConfigManager."""
    mgr = MagicMock()
    mgr.update = AsyncMock()
    mgr.get = AsyncMock(return_value={})
    return mgr


@pytest.fixture()
def event_bus() -> EventBus:
    """Return a fresh EventBus instance."""
    return EventBus()


@pytest.fixture()
def state_manager(
    mock_mqtt: MqttConnectionManager,
    mock_config_manager: MagicMock,
    event_bus: EventBus,
) -> DeviceStateManager:
    """Return a DeviceStateManager with mocked dependencies."""
    return DeviceStateManager(
        mock_mqtt,
        config_manager=mock_config_manager,
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# _parse_color tests
# ---------------------------------------------------------------------------


class TestParseColor:
    """Tests for the _parse_color helper function."""

    def test_hex_with_hash(self):
        assert _parse_color("#FF0080") == (255, 0, 128)

    def test_hex_without_hash(self):
        assert _parse_color("FF0080") == (255, 0, 128)

    def test_hex_lowercase(self):
        assert _parse_color("#ff0080") == (255, 0, 128)

    def test_hex_all_zeros(self):
        assert _parse_color("#000000") == (0, 0, 0)

    def test_hex_all_ff(self):
        assert _parse_color("#FFFFFF") == (255, 255, 255)

    def test_json_rgb_short_keys(self):
        assert _parse_color('{"r":255,"g":0,"b":128}') == (255, 0, 128)

    def test_json_rgb_long_keys(self):
        assert _parse_color('{"red":255,"green":0,"blue":128}') == (255, 0, 128)

    def test_json_clamps_values(self):
        assert _parse_color('{"r":999,"g":-5,"b":128}') == (255, 0, 128)

    def test_json_with_whitespace(self):
        assert _parse_color('  {"r": 10, "g": 20, "b": 30}  ') == (10, 20, 30)

    def test_invalid_string(self):
        assert _parse_color("not a color") is None

    def test_invalid_hex_too_short(self):
        assert _parse_color("#FFF") is None

    def test_invalid_json_array(self):
        assert _parse_color("[255, 0, 0]") is None

    def test_empty_string(self):
        assert _parse_color("") is None


# ---------------------------------------------------------------------------
# Mode mapping tests
# ---------------------------------------------------------------------------


class TestModeMappings:
    """Verify fan/LED mode name <-> int mappings are consistent."""

    def test_fan_mode_names_cover_ints(self):
        for name, val in _FAN_MODE_NAMES.items():
            assert val in _FAN_MODE_INTS

    def test_led_mode_names_cover_ints(self):
        for name, val in _LED_MODE_NAMES.items():
            assert val in _LED_MODE_INTS

    def test_fan_mode_ints_have_5_entries(self):
        assert len(_FAN_MODE_INTS) == 5  # 0-4

    def test_led_mode_ints_have_6_entries(self):
        assert len(_LED_MODE_INTS) == 6  # 0-5


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestDeviceStateManagerInit:
    """Tests for DeviceStateManager construction."""

    def test_default_topic_prefix(self, mock_mqtt, mock_config_manager, event_bus):
        mgr = DeviceStateManager(mock_mqtt, mock_config_manager, event_bus)
        assert mgr.topic_prefix == "casectl"

    def test_custom_topic_prefix(self, mock_mqtt, mock_config_manager, event_bus):
        mgr = DeviceStateManager(
            mock_mqtt, mock_config_manager, event_bus, topic_prefix="myprefix"
        )
        assert mgr.topic_prefix == "myprefix"

    def test_initial_counters(self, state_manager):
        assert state_manager.publish_count == 0
        assert state_manager.command_count == 0
        assert state_manager.error_count == 0

    def test_initially_not_subscribed(self, state_manager):
        assert state_manager.is_subscribed is False


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestDeviceStateManagerLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio()
    async def test_start_subscribes_to_event_bus(self, state_manager, event_bus):
        await state_manager.start()
        assert state_manager.is_subscribed is True
        # Verify event bus has handlers
        assert "fan_state_changed" in event_bus._handlers
        assert "led_state_changed" in event_bus._handlers
        assert "oled_state_changed" in event_bus._handlers

    @pytest.mark.asyncio()
    async def test_start_subscribes_mqtt_command_topics(self, state_manager, mock_mqtt):
        await state_manager.start()
        assert mock_mqtt.subscribe.call_count == 12  # 12 command topics (incl fan curve)

    @pytest.mark.asyncio()
    async def test_start_idempotent(self, state_manager, mock_mqtt):
        await state_manager.start()
        call_count = mock_mqtt.subscribe.call_count
        await state_manager.start()  # second call
        assert mock_mqtt.subscribe.call_count == call_count  # no extra subs

    @pytest.mark.asyncio()
    async def test_stop_unsubscribes(self, state_manager, event_bus, mock_mqtt):
        await state_manager.start()
        await state_manager.stop()
        assert state_manager.is_subscribed is False
        assert "fan_state_changed" not in event_bus._handlers
        assert mock_mqtt.unsubscribe.call_count == 12

    @pytest.mark.asyncio()
    async def test_start_without_event_bus(self, mock_mqtt, mock_config_manager):
        mgr = DeviceStateManager(mock_mqtt, mock_config_manager, event_bus=None)
        await mgr.start()
        assert mgr.is_subscribed is True
        # Only MQTT subscriptions, no event bus
        assert mock_mqtt.subscribe.call_count == 12

    @pytest.mark.asyncio()
    async def test_start_when_mqtt_disconnected(
        self, mock_mqtt, mock_config_manager, event_bus
    ):
        mock_mqtt.is_connected = False
        mgr = DeviceStateManager(mock_mqtt, mock_config_manager, event_bus)
        await mgr.start()
        assert mgr.is_subscribed is True
        # No MQTT subscriptions when disconnected
        assert mock_mqtt.subscribe.call_count == 0


# ---------------------------------------------------------------------------
# Fan state publishing tests
# ---------------------------------------------------------------------------


class TestPublishFanState:
    """Tests for publish_fan_state."""

    @pytest.mark.asyncio()
    async def test_publishes_three_topics(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        assert mock_mqtt.publish.call_count == 3

    @pytest.mark.asyncio()
    async def test_publishes_combined_state(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        first_call = mock_mqtt.publish.call_args_list[0]
        assert first_call[0][0] == "casectl/fan/state"
        payload = json.loads(first_call[0][1])
        assert payload["mode"] == "manual"
        assert payload["duty"] == [128, 128, 128]

    @pytest.mark.asyncio()
    async def test_publishes_mode_topic(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("off", [0, 0, 0])
        mode_call = mock_mqtt.publish.call_args_list[1]
        assert mode_call[0][0] == "casectl/fan/mode"
        assert mode_call[0][1] == "off"

    @pytest.mark.asyncio()
    async def test_publishes_duty_as_percentage(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("manual", [255, 128, 0])
        duty_call = mock_mqtt.publish.call_args_list[2]
        assert duty_call[0][0] == "casectl/fan/duty"
        duty = json.loads(duty_call[0][1])
        assert duty == [100, 50, 0]

    @pytest.mark.asyncio()
    async def test_deduplicates_unchanged_state(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        mock_mqtt.publish.reset_mock()
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        assert mock_mqtt.publish.call_count == 0

    @pytest.mark.asyncio()
    async def test_force_publishes_unchanged(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        mock_mqtt.publish.reset_mock()
        await state_manager.publish_fan_state("manual", [128, 128, 128], force=True)
        assert mock_mqtt.publish.call_count == 3

    @pytest.mark.asyncio()
    async def test_skips_when_disconnected(self, state_manager, mock_mqtt):
        mock_mqtt.is_connected = False
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        assert mock_mqtt.publish.call_count == 0

    @pytest.mark.asyncio()
    async def test_increments_publish_count(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        assert state_manager.publish_count == 1

    @pytest.mark.asyncio()
    async def test_handles_publish_error(self, state_manager, mock_mqtt):
        mock_mqtt.publish = AsyncMock(side_effect=RuntimeError("broker down"))
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        assert state_manager.error_count == 1
        assert state_manager.publish_count == 0


# ---------------------------------------------------------------------------
# LED state publishing tests
# ---------------------------------------------------------------------------


class TestPublishLedState:
    """Tests for publish_led_state."""

    @pytest.mark.asyncio()
    async def test_publishes_four_topics(self, state_manager, mock_mqtt):
        color = {"red": 255, "green": 0, "blue": 128}
        await state_manager.publish_led_state("manual", color)
        assert mock_mqtt.publish.call_count == 4  # state, mode, color, brightness

    @pytest.mark.asyncio()
    async def test_publishes_combined_state(self, state_manager, mock_mqtt):
        color = {"red": 255, "green": 0, "blue": 128}
        await state_manager.publish_led_state("manual", color)
        first_call = mock_mqtt.publish.call_args_list[0]
        assert first_call[0][0] == "casectl/led/state"
        payload = json.loads(first_call[0][1])
        assert payload["mode"] == "manual"
        assert payload["color"] == color

    @pytest.mark.asyncio()
    async def test_publishes_mode_topic(self, state_manager, mock_mqtt):
        color = {"red": 0, "green": 0, "blue": 0}
        await state_manager.publish_led_state("rainbow", color)
        mode_call = mock_mqtt.publish.call_args_list[1]
        assert mode_call[0][0] == "casectl/led/mode"
        assert mode_call[0][1] == "rainbow"

    @pytest.mark.asyncio()
    async def test_publishes_color_compact(self, state_manager, mock_mqtt):
        color = {"red": 255, "green": 128, "blue": 0}
        await state_manager.publish_led_state("manual", color)
        color_call = mock_mqtt.publish.call_args_list[2]
        assert color_call[0][0] == "casectl/led/color"
        parsed = json.loads(color_call[0][1])
        assert parsed == {"r": 255, "g": 128, "b": 0}

    @pytest.mark.asyncio()
    async def test_publishes_brightness(self, state_manager, mock_mqtt):
        color = {"red": 100, "green": 200, "blue": 50}
        await state_manager.publish_led_state("manual", color)
        brightness_call = mock_mqtt.publish.call_args_list[3]
        assert brightness_call[0][0] == "casectl/led/brightness"
        assert brightness_call[0][1] == "200"  # max channel

    @pytest.mark.asyncio()
    async def test_deduplicates_unchanged_state(self, state_manager, mock_mqtt):
        color = {"red": 255, "green": 0, "blue": 0}
        await state_manager.publish_led_state("manual", color)
        mock_mqtt.publish.reset_mock()
        await state_manager.publish_led_state("manual", color)
        assert mock_mqtt.publish.call_count == 0

    @pytest.mark.asyncio()
    async def test_skips_when_disconnected(self, state_manager, mock_mqtt):
        mock_mqtt.is_connected = False
        await state_manager.publish_led_state("manual", {"red": 0, "green": 0, "blue": 0})
        assert mock_mqtt.publish.call_count == 0

    @pytest.mark.asyncio()
    async def test_handles_publish_error(self, state_manager, mock_mqtt):
        mock_mqtt.publish = AsyncMock(side_effect=RuntimeError("broker down"))
        await state_manager.publish_led_state("manual", {"red": 0, "green": 0, "blue": 0})
        assert state_manager.error_count == 1


# ---------------------------------------------------------------------------
# OLED state publishing tests
# ---------------------------------------------------------------------------


class TestPublishOledState:
    """Tests for publish_oled_state."""

    @pytest.mark.asyncio()
    async def test_publishes_three_topics(self, state_manager, mock_mqtt):
        await state_manager.publish_oled_state(0, 2)
        assert mock_mqtt.publish.call_count == 3

    @pytest.mark.asyncio()
    async def test_publishes_combined_state(self, state_manager, mock_mqtt):
        await state_manager.publish_oled_state(180, 1)
        first_call = mock_mqtt.publish.call_args_list[0]
        assert first_call[0][0] == "casectl/oled/state"
        payload = json.loads(first_call[0][1])
        assert payload == {"rotation": 180, "current_screen": 1}

    @pytest.mark.asyncio()
    async def test_publishes_rotation_topic(self, state_manager, mock_mqtt):
        await state_manager.publish_oled_state(180, 0)
        rotation_call = mock_mqtt.publish.call_args_list[1]
        assert rotation_call[0][0] == "casectl/oled/rotation"
        assert rotation_call[0][1] == "180"

    @pytest.mark.asyncio()
    async def test_publishes_screen_topic(self, state_manager, mock_mqtt):
        await state_manager.publish_oled_state(0, 3)
        screen_call = mock_mqtt.publish.call_args_list[2]
        assert screen_call[0][0] == "casectl/oled/screen"
        assert screen_call[0][1] == "3"

    @pytest.mark.asyncio()
    async def test_deduplicates_unchanged_state(self, state_manager, mock_mqtt):
        await state_manager.publish_oled_state(0, 0)
        mock_mqtt.publish.reset_mock()
        await state_manager.publish_oled_state(0, 0)
        assert mock_mqtt.publish.call_count == 0

    @pytest.mark.asyncio()
    async def test_force_publishes_unchanged(self, state_manager, mock_mqtt):
        await state_manager.publish_oled_state(0, 0)
        mock_mqtt.publish.reset_mock()
        await state_manager.publish_oled_state(0, 0, force=True)
        assert mock_mqtt.publish.call_count == 3

    @pytest.mark.asyncio()
    async def test_handles_publish_error(self, state_manager, mock_mqtt):
        mock_mqtt.publish = AsyncMock(side_effect=RuntimeError("broker down"))
        await state_manager.publish_oled_state(0, 0)
        assert state_manager.error_count == 1


# ---------------------------------------------------------------------------
# Event bus handler tests
# ---------------------------------------------------------------------------


class TestEventBusHandlers:
    """Tests for event bus integration."""

    @pytest.mark.asyncio()
    async def test_fan_state_changed_event(self, state_manager, event_bus, mock_mqtt):
        await state_manager.start()
        await event_bus.emit(
            "fan_state_changed", {"mode": "manual", "duty": [64, 64, 64]}
        )
        assert mock_mqtt.publish.call_count == 3

    @pytest.mark.asyncio()
    async def test_led_state_changed_event(self, state_manager, event_bus, mock_mqtt):
        await state_manager.start()
        await event_bus.emit(
            "led_state_changed",
            {"mode": "rainbow", "color": {"red": 0, "green": 0, "blue": 0}},
        )
        assert mock_mqtt.publish.call_count == 4

    @pytest.mark.asyncio()
    async def test_oled_state_changed_event(self, state_manager, event_bus, mock_mqtt):
        await state_manager.start()
        await event_bus.emit(
            "oled_state_changed", {"rotation": 180, "current_screen": 2}
        )
        assert mock_mqtt.publish.call_count == 3

    @pytest.mark.asyncio()
    async def test_event_with_missing_fields_uses_defaults(
        self, state_manager, event_bus, mock_mqtt
    ):
        await state_manager.start()
        await event_bus.emit("fan_state_changed", {})
        first_call = mock_mqtt.publish.call_args_list[0]
        payload = json.loads(first_call[0][1])
        assert payload["mode"] == "unknown"
        assert payload["duty"] == [0, 0, 0]


# ---------------------------------------------------------------------------
# Fan command handler tests
# ---------------------------------------------------------------------------


class TestFanModeCommand:
    """Tests for _handle_fan_mode_command."""

    @pytest.mark.asyncio()
    async def test_set_mode_by_name(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"manual"
        )
        mock_config_manager.update.assert_awaited_once_with("fan", {"mode": 2})
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_set_mode_by_name_follow_temp(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"follow-temp"
        )
        mock_config_manager.update.assert_awaited_once_with("fan", {"mode": 0})

    @pytest.mark.asyncio()
    async def test_set_mode_by_integer(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"4"
        )
        mock_config_manager.update.assert_awaited_once_with("fan", {"mode": 4})

    @pytest.mark.asyncio()
    async def test_set_mode_uppercase(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"MANUAL"
        )
        mock_config_manager.update.assert_awaited_once_with("fan", {"mode": 2})

    @pytest.mark.asyncio()
    async def test_invalid_mode_name(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"turbo"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_invalid_mode_int(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"99"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_config_update_error(self, state_manager, mock_config_manager):
        mock_config_manager.update = AsyncMock(side_effect=RuntimeError("disk full"))
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"manual"
        )
        assert state_manager.error_count == 1
        assert state_manager.command_count == 0

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_fan_mode_command("t", b"manual")
        assert mgr.command_count == 0


class TestFanDutyCommand:
    """Tests for _handle_fan_duty_command."""

    @pytest.mark.asyncio()
    async def test_json_array(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"[50, 75, 100]"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[0] == "fan"
        assert call_args[1]["manual_duty"] == [127, 191, 255]
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_single_integer(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"80"
        )
        call_args = mock_config_manager.update.call_args[0]
        # 80% of 255 = 204, padded to 3 channels
        assert call_args[1]["manual_duty"] == [204, 204, 204]

    @pytest.mark.asyncio()
    async def test_json_single_value(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"50"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["manual_duty"] == [127, 127, 127]

    @pytest.mark.asyncio()
    async def test_clamps_values(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"[150, -10, 50]"
        )
        call_args = mock_config_manager.update.call_args[0]
        # 100% -> 255, 0% -> 0, 50% -> 127
        assert call_args[1]["manual_duty"] == [255, 0, 127]

    @pytest.mark.asyncio()
    async def test_pads_single_channel(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"[50]"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert len(call_args[1]["manual_duty"]) == 3
        assert call_args[1]["manual_duty"] == [127, 127, 127]

    @pytest.mark.asyncio()
    async def test_truncates_extra_channels(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"[50, 60, 70, 80]"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert len(call_args[1]["manual_duty"]) == 3

    @pytest.mark.asyncio()
    async def test_switches_to_manual_mode(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"[50, 50, 50]"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["mode"] == 2  # FanMode.MANUAL

    @pytest.mark.asyncio()
    async def test_invalid_payload(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"not a number"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_fan_duty_command("t", b"[50]")
        assert mgr.command_count == 0


# ---------------------------------------------------------------------------
# LED command handler tests
# ---------------------------------------------------------------------------


class TestLedModeCommand:
    """Tests for _handle_led_mode_command."""

    @pytest.mark.asyncio()
    async def test_set_mode_by_name(self, state_manager, mock_config_manager):
        await state_manager._handle_led_mode_command(
            "casectl/led/mode/set", b"rainbow"
        )
        mock_config_manager.update.assert_awaited_once_with("led", {"mode": 0})

    @pytest.mark.asyncio()
    async def test_set_mode_by_integer(self, state_manager, mock_config_manager):
        await state_manager._handle_led_mode_command(
            "casectl/led/mode/set", b"5"
        )
        mock_config_manager.update.assert_awaited_once_with("led", {"mode": 5})

    @pytest.mark.asyncio()
    async def test_set_mode_follow_temp(self, state_manager, mock_config_manager):
        await state_manager._handle_led_mode_command(
            "casectl/led/mode/set", b"follow-temp"
        )
        mock_config_manager.update.assert_awaited_once_with("led", {"mode": 2})

    @pytest.mark.asyncio()
    async def test_invalid_mode(self, state_manager, mock_config_manager):
        await state_manager._handle_led_mode_command(
            "casectl/led/mode/set", b"disco"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_led_mode_command("t", b"rainbow")
        assert mgr.command_count == 0


class TestLedColorCommand:
    """Tests for _handle_led_color_command."""

    @pytest.mark.asyncio()
    async def test_hex_color(self, state_manager, mock_config_manager):
        await state_manager._handle_led_color_command(
            "casectl/led/color/set", b"#FF0080"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["red_value"] == 255
        assert call_args[1]["green_value"] == 0
        assert call_args[1]["blue_value"] == 128
        assert call_args[1]["mode"] == 3  # LedMode.MANUAL

    @pytest.mark.asyncio()
    async def test_json_color(self, state_manager, mock_config_manager):
        await state_manager._handle_led_color_command(
            "casectl/led/color/set", b'{"r":10,"g":20,"b":30}'
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["red_value"] == 10
        assert call_args[1]["green_value"] == 20
        assert call_args[1]["blue_value"] == 30

    @pytest.mark.asyncio()
    async def test_invalid_color(self, state_manager, mock_config_manager):
        await state_manager._handle_led_color_command(
            "casectl/led/color/set", b"not-a-color"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_switches_to_manual_mode(self, state_manager, mock_config_manager):
        await state_manager._handle_led_color_command(
            "casectl/led/color/set", b"#FF0000"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["mode"] == 3  # LedMode.MANUAL

    @pytest.mark.asyncio()
    async def test_config_error(self, state_manager, mock_config_manager):
        mock_config_manager.update = AsyncMock(side_effect=RuntimeError("fail"))
        await state_manager._handle_led_color_command(
            "casectl/led/color/set", b"#FF0000"
        )
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_led_color_command("t", b"#FF0000")
        assert mgr.command_count == 0


# ---------------------------------------------------------------------------
# OLED command handler tests
# ---------------------------------------------------------------------------


class TestOledRotationCommand:
    """Tests for _handle_oled_rotation_command."""

    @pytest.mark.asyncio()
    async def test_set_rotation_0(self, state_manager, mock_config_manager):
        await state_manager._handle_oled_rotation_command(
            "casectl/oled/rotation/set", b"0"
        )
        mock_config_manager.update.assert_awaited_once_with("oled", {"rotation": 0})

    @pytest.mark.asyncio()
    async def test_set_rotation_180(self, state_manager, mock_config_manager):
        await state_manager._handle_oled_rotation_command(
            "casectl/oled/rotation/set", b"180"
        )
        mock_config_manager.update.assert_awaited_once_with("oled", {"rotation": 180})

    @pytest.mark.asyncio()
    async def test_invalid_rotation_value(self, state_manager, mock_config_manager):
        await state_manager._handle_oled_rotation_command(
            "casectl/oled/rotation/set", b"90"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_invalid_non_integer(self, state_manager, mock_config_manager):
        await state_manager._handle_oled_rotation_command(
            "casectl/oled/rotation/set", b"abc"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_oled_rotation_command("t", b"0")
        assert mgr.command_count == 0


class TestOledScreenCommand:
    """Tests for _handle_oled_screen_command."""

    @pytest.mark.asyncio()
    async def test_set_screen_valid(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"screens": [{"enabled": False}, {"enabled": False}, {"enabled": False}]}
        )
        await state_manager._handle_oled_screen_command(
            "casectl/oled/screen/set", b"1"
        )
        mock_config_manager.update.assert_awaited_once()
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["screens"][1]["enabled"] is True
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_screen_index_out_of_range(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"screens": [{"enabled": False}]}
        )
        await state_manager._handle_oled_screen_command(
            "casectl/oled/screen/set", b"3"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_screen_negative_index(self, state_manager, mock_config_manager):
        await state_manager._handle_oled_screen_command(
            "casectl/oled/screen/set", b"-1"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_screen_index_too_high(self, state_manager, mock_config_manager):
        await state_manager._handle_oled_screen_command(
            "casectl/oled/screen/set", b"5"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_invalid_non_integer(self, state_manager, mock_config_manager):
        await state_manager._handle_oled_screen_command(
            "casectl/oled/screen/set", b"abc"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_config_error(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(side_effect=RuntimeError("fail"))
        await state_manager._handle_oled_screen_command(
            "casectl/oled/screen/set", b"0"
        )
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_oled_screen_command("t", b"0")
        assert mgr.command_count == 0


# ---------------------------------------------------------------------------
# HA discovery entity tests
# ---------------------------------------------------------------------------


class TestCommandEntities:
    """Tests for get_command_entities and COMMAND_ENTITIES."""

    def test_command_entities_defined(self):
        assert len(COMMAND_ENTITIES) == 6

    def test_get_command_entities_fills_command_topics(self, state_manager):
        entities = state_manager.get_command_entities()
        for entity in entities:
            if "command_topic" in entity.extra:
                assert entity.extra["command_topic"] != ""
                assert entity.extra["command_topic"].startswith("casectl/")

    def test_fan_mode_entity(self, state_manager):
        entities = state_manager.get_command_entities()
        fan_mode = next(e for e in entities if e.object_id == "fan_mode")
        assert fan_mode.component == "select"
        assert fan_mode.extra["command_topic"] == "casectl/fan/mode/set"
        assert "manual" in fan_mode.extra["options"]

    def test_led_mode_entity(self, state_manager):
        entities = state_manager.get_command_entities()
        led_mode = next(e for e in entities if e.object_id == "led_mode")
        assert led_mode.component == "select"
        assert led_mode.extra["command_topic"] == "casectl/led/mode/set"

    def test_led_light_entity(self, state_manager):
        entities = state_manager.get_command_entities()
        led_light = next(e for e in entities if e.object_id == "led_light")
        assert led_light.component == "light"
        assert led_light.extra["command_topic"] == "casectl/led/light/set"

    def test_oled_rotation_entity(self, state_manager):
        entities = state_manager.get_command_entities()
        oled_rot = next(e for e in entities if e.object_id == "oled_rotation")
        assert oled_rot.component == "select"
        assert oled_rot.extra["command_topic"] == "casectl/oled/rotation/set"

    def test_custom_prefix(self, mock_mqtt, mock_config_manager, event_bus):
        mgr = DeviceStateManager(
            mock_mqtt, mock_config_manager, event_bus, topic_prefix="mypi"
        )
        entities = mgr.get_command_entities()
        fan_mode = next(e for e in entities if e.object_id == "fan_mode")
        assert fan_mode.extra["command_topic"] == "mypi/fan/mode/set"


# ---------------------------------------------------------------------------
# Status / diagnostic tests
# ---------------------------------------------------------------------------


class TestGetStatus:
    """Tests for get_status."""

    def test_initial_status(self, state_manager):
        status = state_manager.get_status()
        assert status["subscribed"] is False
        assert status["publish_count"] == 0
        assert status["command_count"] == 0
        assert status["error_count"] == 0
        assert status["topic_prefix"] == "casectl"

    @pytest.mark.asyncio()
    async def test_status_after_start(self, state_manager):
        await state_manager.start()
        status = state_manager.get_status()
        assert status["subscribed"] is True

    @pytest.mark.asyncio()
    async def test_status_after_publish(self, state_manager, mock_mqtt):
        await state_manager.publish_fan_state("manual", [128, 128, 128])
        status = state_manager.get_status()
        assert status["publish_count"] == 1

    @pytest.mark.asyncio()
    async def test_status_after_command(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_mode_command("t", b"manual")
        status = state_manager.get_status()
        assert status["command_count"] == 1


# ---------------------------------------------------------------------------
# Per-channel fan duty command handler tests
# ---------------------------------------------------------------------------


class TestFanChannelDutyCommand:
    """Tests for _handle_fan_channel_duty_command (per-channel duty)."""

    @pytest.mark.asyncio()
    async def test_set_channel_0_duty(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [75, 75, 75]}
        )
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/0/set", b"50", 0
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[0] == "fan"
        # 50% of 255 = 127
        assert call_args[1]["manual_duty"][0] == 127
        # Other channels preserved
        assert call_args[1]["manual_duty"][1] == 75
        assert call_args[1]["manual_duty"][2] == 75
        assert call_args[1]["mode"] == 2  # FanMode.MANUAL
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_set_channel_1_duty(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [100, 100, 100]}
        )
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/1/set", b"80", 1
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["manual_duty"][0] == 100
        assert call_args[1]["manual_duty"][1] == 204  # 80% of 255
        assert call_args[1]["manual_duty"][2] == 100

    @pytest.mark.asyncio()
    async def test_set_channel_2_duty(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [50, 50, 50]}
        )
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/2/set", b"100", 2
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["manual_duty"][2] == 255  # 100% of 255

    @pytest.mark.asyncio()
    async def test_clamps_to_0_100(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [75, 75, 75]}
        )
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/0/set", b"150", 0
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["manual_duty"][0] == 255  # clamped to 100% = 255

    @pytest.mark.asyncio()
    async def test_accepts_float(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [75, 75, 75]}
        )
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/0/set", b"50.5", 0
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["manual_duty"][0] == 127  # 50% of 255

    @pytest.mark.asyncio()
    async def test_invalid_payload(self, state_manager, mock_config_manager):
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/0/set", b"not-a-number", 0
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_fan_channel_duty_command("t", b"50", 0)
        assert mgr.command_count == 0

    @pytest.mark.asyncio()
    async def test_config_read_error(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(side_effect=RuntimeError("fail"))
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/0/set", b"50", 0
        )
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_pads_short_duty_array(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [100]}
        )
        await state_manager._handle_fan_channel_duty_command(
            "casectl/fan/duty/2/set", b"50", 2
        )
        call_args = mock_config_manager.update.call_args[0]
        assert len(call_args[1]["manual_duty"]) == 3

    @pytest.mark.asyncio()
    async def test_handler_factory(self, state_manager, mock_config_manager):
        """Test that _make_channel_duty_handler returns a working handler."""
        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [75, 75, 75]}
        )
        handler = state_manager._make_channel_duty_handler(1)
        await handler("casectl/fan/duty/1/set", b"60")
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["manual_duty"][1] == 153  # 60% of 255


# ---------------------------------------------------------------------------
# LED light command handler tests (HA JSON schema)
# ---------------------------------------------------------------------------


class TestLedLightCommand:
    """Tests for _handle_led_light_command (Home Assistant JSON light schema)."""

    @pytest.mark.asyncio()
    async def test_state_off(self, state_manager, mock_config_manager):
        await state_manager._handle_led_light_command(
            "casectl/led/light/set", b'{"state": "OFF"}'
        )
        mock_config_manager.update.assert_awaited_once_with("led", {"mode": 5})
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_state_on_with_color(self, state_manager, mock_config_manager):
        await state_manager._handle_led_light_command(
            "casectl/led/light/set",
            b'{"state": "ON", "color": {"r": 255, "g": 0, "b": 128}}',
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["mode"] == 3  # LedMode.MANUAL
        assert call_args[1]["red_value"] == 255
        assert call_args[1]["green_value"] == 0
        assert call_args[1]["blue_value"] == 128
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_color_only_no_state(self, state_manager, mock_config_manager):
        """When state is omitted, defaults to ON."""
        await state_manager._handle_led_light_command(
            "casectl/led/light/set",
            b'{"color": {"r": 0, "g": 255, "b": 0}}',
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["mode"] == 3
        assert call_args[1]["green_value"] == 255

    @pytest.mark.asyncio()
    async def test_brightness_only(self, state_manager, mock_config_manager):
        """Brightness without colour should scale the current LED colour."""
        mock_config_manager.get = AsyncMock(
            return_value={"red_value": 255, "green_value": 0, "blue_value": 0}
        )
        await state_manager._handle_led_light_command(
            "casectl/led/light/set", b'{"brightness": 128}'
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["red_value"] == 128
        assert call_args[1]["green_value"] == 0
        assert call_args[1]["blue_value"] == 0

    @pytest.mark.asyncio()
    async def test_color_and_brightness(self, state_manager, mock_config_manager):
        """Color + brightness should scale the provided colour to the brightness."""
        await state_manager._handle_led_light_command(
            "casectl/led/light/set",
            b'{"color": {"r": 255, "g": 0, "b": 0}, "brightness": 128}',
        )
        call_args = mock_config_manager.update.call_args[0]
        # 255 * (128/255) ≈ 128
        assert call_args[1]["red_value"] == 128
        assert call_args[1]["green_value"] == 0
        assert call_args[1]["blue_value"] == 0

    @pytest.mark.asyncio()
    async def test_full_brightness_rgb(self, state_manager, mock_config_manager):
        """Full brightness should preserve the colour."""
        await state_manager._handle_led_light_command(
            "casectl/led/light/set",
            b'{"color": {"r": 200, "g": 100, "b": 50}, "brightness": 200}',
        )
        call_args = mock_config_manager.update.call_args[0]
        # max channel is 200, brightness is 200 → scale = 1.0
        assert call_args[1]["red_value"] == 200
        assert call_args[1]["green_value"] == 100
        assert call_args[1]["blue_value"] == 50

    @pytest.mark.asyncio()
    async def test_invalid_json(self, state_manager, mock_config_manager):
        await state_manager._handle_led_light_command(
            "casectl/led/light/set", b"not json"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_json_array_rejected(self, state_manager, mock_config_manager):
        await state_manager._handle_led_light_command(
            "casectl/led/light/set", b"[1, 2, 3]"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_led_light_command("t", b'{"state": "ON"}')
        assert mgr.command_count == 0

    @pytest.mark.asyncio()
    async def test_config_update_error(self, state_manager, mock_config_manager):
        mock_config_manager.update = AsyncMock(side_effect=RuntimeError("fail"))
        await state_manager._handle_led_light_command(
            "casectl/led/light/set",
            b'{"state": "ON", "color": {"r": 255, "g": 0, "b": 0}}',
        )
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_state_off_lowercase(self, state_manager, mock_config_manager):
        """State comparison should be case-insensitive."""
        await state_manager._handle_led_light_command(
            "casectl/led/light/set", b'{"state": "off"}'
        )
        mock_config_manager.update.assert_awaited_once_with("led", {"mode": 5})


# ---------------------------------------------------------------------------
# LED brightness command handler tests
# ---------------------------------------------------------------------------


class TestLedBrightnessCommand:
    """Tests for _handle_led_brightness_command."""

    @pytest.mark.asyncio()
    async def test_set_brightness(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"red_value": 255, "green_value": 0, "blue_value": 0}
        )
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"128"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["mode"] == 3  # MANUAL
        assert call_args[1]["red_value"] == 128
        assert call_args[1]["green_value"] == 0
        assert call_args[1]["blue_value"] == 0
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_brightness_zero_turns_off(self, state_manager, mock_config_manager):
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"0"
        )
        mock_config_manager.update.assert_awaited_once_with("led", {"mode": 5})
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_brightness_max(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"red_value": 100, "green_value": 50, "blue_value": 25}
        )
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"255"
        )
        call_args = mock_config_manager.update.call_args[0]
        # max channel is 100, brightness 255 → scale = 2.55
        assert call_args[1]["red_value"] == 255
        assert call_args[1]["green_value"] == 127  # round(50 * 2.549...) = 127
        assert call_args[1]["blue_value"] == 64    # round(25 * 2.549...) = 64

    @pytest.mark.asyncio()
    async def test_brightness_clamps_over_255(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"red_value": 255, "green_value": 0, "blue_value": 0}
        )
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"300"
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["red_value"] == 255  # clamped

    @pytest.mark.asyncio()
    async def test_accepts_float(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(
            return_value={"red_value": 255, "green_value": 0, "blue_value": 0}
        )
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"128.5"
        )
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_invalid_payload(self, state_manager, mock_config_manager):
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"not-a-number"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        mgr = DeviceStateManager(mock_mqtt, config_manager=None, event_bus=event_bus)
        await mgr._handle_led_brightness_command("t", b"128")
        assert mgr.command_count == 0

    @pytest.mark.asyncio()
    async def test_config_error(self, state_manager, mock_config_manager):
        mock_config_manager.get = AsyncMock(side_effect=RuntimeError("fail"))
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"128"
        )
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_default_color_when_no_config(self, state_manager, mock_config_manager):
        """When config returns non-dict, uses default blue."""
        mock_config_manager.get = AsyncMock(return_value=None)
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"128"
        )
        call_args = mock_config_manager.update.call_args[0]
        # Default is r=0, g=0, b=255 → scaled to 128
        assert call_args[1]["blue_value"] == 128


# ---------------------------------------------------------------------------
# Fan curve command tests
# ---------------------------------------------------------------------------


class TestFanCurveCommand:
    """Tests for the fan curve set command handler."""

    @pytest.mark.asyncio()
    async def test_valid_curve_two_points(self, state_manager, mock_config_manager, mock_mqtt):
        """Accept a minimal 2-point curve."""
        payload = json.dumps([{"temp": 30, "duty": 20}, {"temp": 60, "duty": 80}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        mock_config_manager.update.assert_awaited_once()
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[0] == "fan"
        assert call_args[1]["curve_points"] == [
            {"temp": 30, "duty": 20},
            {"temp": 60, "duty": 80},
        ]
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_valid_curve_switches_to_custom_mode(
        self, state_manager, mock_config_manager, mock_mqtt
    ):
        """The fan mode should be set to CUSTOM (3)."""
        payload = json.dumps([{"temp": 30, "duty": 20}, {"temp": 60, "duty": 80}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["mode"] == 3  # FanMode.CUSTOM

    @pytest.mark.asyncio()
    async def test_valid_curve_publishes_state(self, state_manager, mock_config_manager, mock_mqtt):
        """After applying, should publish curve to MQTT."""
        payload = json.dumps([{"temp": 30, "duty": 20}, {"temp": 60, "duty": 80}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        # Should have published to the fan/curve topic
        publish_calls = mock_mqtt.publish.call_args_list
        curve_publish = [c for c in publish_calls if "fan/curve" in str(c)]
        assert len(curve_publish) == 1

    @pytest.mark.asyncio()
    async def test_three_point_curve(self, state_manager, mock_config_manager, mock_mqtt):
        """Accept a 3-point curve with increasing temperatures."""
        payload = json.dumps([
            {"temp": 25, "duty": 10},
            {"temp": 45, "duty": 50},
            {"temp": 65, "duty": 100},
        ])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        call_args = mock_config_manager.update.call_args[0]
        assert len(call_args[1]["curve_points"]) == 3
        assert state_manager.command_count == 1

    @pytest.mark.asyncio()
    async def test_clamps_duty_to_100(self, state_manager, mock_config_manager, mock_mqtt):
        """Duty values above 100 should be clamped."""
        payload = json.dumps([{"temp": 30, "duty": 50}, {"temp": 60, "duty": 150}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["curve_points"][1]["duty"] == 100

    @pytest.mark.asyncio()
    async def test_clamps_duty_to_0(self, state_manager, mock_config_manager, mock_mqtt):
        """Duty values below 0 should be clamped."""
        payload = json.dumps([{"temp": 30, "duty": -10}, {"temp": 60, "duty": 50}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["curve_points"][0]["duty"] == 0

    @pytest.mark.asyncio()
    async def test_rejects_single_point(self, state_manager, mock_config_manager):
        """A curve with fewer than 2 points should be rejected."""
        payload = json.dumps([{"temp": 30, "duty": 20}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_empty_array(self, state_manager, mock_config_manager):
        """An empty array should be rejected."""
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", b"[]"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_too_many_points(self, state_manager, mock_config_manager):
        """More than MAX_CURVE_POINTS should be rejected."""
        points = [{"temp": i * 5, "duty": i * 4} for i in range(MAX_CURVE_POINTS + 1)]
        payload = json.dumps(points)
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_non_increasing_temps(self, state_manager, mock_config_manager):
        """Temperatures must be strictly increasing."""
        payload = json.dumps([{"temp": 50, "duty": 20}, {"temp": 50, "duty": 80}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_decreasing_temps(self, state_manager, mock_config_manager):
        """Decreasing temperatures should be rejected."""
        payload = json.dumps([{"temp": 60, "duty": 80}, {"temp": 30, "duty": 20}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_non_json(self, state_manager, mock_config_manager):
        """Invalid JSON should be rejected."""
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", b"not json"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_json_object(self, state_manager, mock_config_manager):
        """A JSON object (not array) should be rejected."""
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", b'{"temp": 30, "duty": 20}'
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_non_object_points(self, state_manager, mock_config_manager):
        """Points that are not objects should be rejected."""
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", b"[30, 60]"
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_rejects_invalid_values(self, state_manager, mock_config_manager):
        """Points with non-numeric values should be rejected."""
        payload = json.dumps([{"temp": "hot", "duty": 20}, {"temp": 60, "duty": 80}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        mock_config_manager.update.assert_not_awaited()
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_config_update_error(self, state_manager, mock_config_manager, mock_mqtt):
        """Config manager errors should be handled gracefully."""
        mock_config_manager.update = AsyncMock(side_effect=RuntimeError("fail"))
        payload = json.dumps([{"temp": 30, "duty": 20}, {"temp": 60, "duty": 80}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        assert state_manager.error_count == 1

    @pytest.mark.asyncio()
    async def test_no_config_manager(self, mock_mqtt, event_bus):
        """Should handle missing config manager gracefully."""
        mgr = DeviceStateManager(mock_mqtt, event_bus=event_bus)
        await mgr._handle_fan_curve_command(
            "casectl/fan/curve/set",
            json.dumps([{"temp": 30, "duty": 20}, {"temp": 60, "duty": 80}]).encode(),
        )
        assert mgr.command_count == 0

    @pytest.mark.asyncio()
    async def test_alternative_key_names(self, state_manager, mock_config_manager, mock_mqtt):
        """Should accept 'temperature' and 'duty_percent' keys."""
        payload = json.dumps([
            {"temperature": 30, "duty_percent": 20},
            {"temperature": 60, "duty_percent": 80},
        ])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        call_args = mock_config_manager.update.call_args[0]
        assert call_args[1]["curve_points"] == [
            {"temp": 30, "duty": 20},
            {"temp": 60, "duty": 80},
        ]

    @pytest.mark.asyncio()
    async def test_max_curve_points_accepted(self, state_manager, mock_config_manager, mock_mqtt):
        """Exactly MAX_CURVE_POINTS should be accepted."""
        points = [{"temp": i * 5, "duty": min(100, i * 5)} for i in range(MAX_CURVE_POINTS)]
        payload = json.dumps(points)
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        assert state_manager.command_count == 1
        call_args = mock_config_manager.update.call_args[0]
        assert len(call_args[1]["curve_points"]) == MAX_CURVE_POINTS


# ---------------------------------------------------------------------------
# Command event emission tests
# ---------------------------------------------------------------------------


class TestCommandEventEmission:
    """Tests for mqtt_command_received event bus emission."""

    @pytest.mark.asyncio()
    async def test_fan_mode_emits_event(self, state_manager, mock_config_manager, event_bus):
        """Fan mode command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"manual"
        )
        # Allow event bus to dispatch
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "fan"
        assert received[0]["action"] == "mode"
        assert received[0]["value"] == "manual"
        assert received[0]["source"] == "mqtt"
        assert received[0]["topic"] == "casectl/fan/mode/set"

    @pytest.mark.asyncio()
    async def test_fan_duty_emits_event(self, state_manager, mock_config_manager, event_bus):
        """Fan duty command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_fan_duty_command(
            "casectl/fan/duty/set", b"[50,60,70]"
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "fan"
        assert received[0]["action"] == "duty"
        assert received[0]["value"] == [50, 60, 70]

    @pytest.mark.asyncio()
    async def test_led_mode_emits_event(self, state_manager, mock_config_manager, event_bus):
        """LED mode command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_led_mode_command(
            "casectl/led/mode/set", b"rainbow"
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "led"
        assert received[0]["action"] == "mode"
        assert received[0]["value"] == "rainbow"

    @pytest.mark.asyncio()
    async def test_led_color_emits_event(self, state_manager, mock_config_manager, event_bus):
        """LED color command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_led_color_command(
            "casectl/led/color/set", b"#FF0000"
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "led"
        assert received[0]["action"] == "color"
        assert received[0]["value"] == {"r": 255, "g": 0, "b": 0}

    @pytest.mark.asyncio()
    async def test_led_light_off_emits_event(self, state_manager, mock_config_manager, event_bus):
        """LED light OFF command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_led_light_command(
            "casectl/led/light/set", b'{"state": "OFF"}'
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "led"
        assert received[0]["action"] == "light"
        assert received[0]["value"]["state"] == "OFF"

    @pytest.mark.asyncio()
    async def test_led_brightness_emits_event(self, state_manager, mock_config_manager, event_bus):
        """LED brightness command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_led_brightness_command(
            "casectl/led/brightness/set", b"200"
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "led"
        assert received[0]["action"] == "brightness"
        assert received[0]["value"] == 200

    @pytest.mark.asyncio()
    async def test_oled_rotation_emits_event(self, state_manager, mock_config_manager, event_bus):
        """OLED rotation command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_oled_rotation_command(
            "casectl/oled/rotation/set", b"180"
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "oled"
        assert received[0]["action"] == "rotation"
        assert received[0]["value"] == 180

    @pytest.mark.asyncio()
    async def test_oled_screen_emits_event(
        self, state_manager, mock_config_manager, event_bus
    ):
        """OLED screen command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        mock_config_manager.get = AsyncMock(
            return_value={"screens": [{"enabled": False}, {"enabled": False}]}
        )

        await state_manager.start()
        await state_manager._handle_oled_screen_command(
            "casectl/oled/screen/set", b"1"
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "oled"
        assert received[0]["action"] == "screen"
        assert received[0]["value"] == 1

    @pytest.mark.asyncio()
    async def test_fan_curve_emits_event(
        self, state_manager, mock_config_manager, event_bus, mock_mqtt
    ):
        """Fan curve command should emit mqtt_command_received event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        payload = json.dumps([{"temp": 30, "duty": 20}, {"temp": 60, "duty": 80}])
        await state_manager._handle_fan_curve_command(
            "casectl/fan/curve/set", payload.encode()
        )
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "fan"
        assert received[0]["action"] == "curve"
        assert len(received[0]["value"]) == 2

    @pytest.mark.asyncio()
    async def test_no_event_on_error(self, state_manager, mock_config_manager, event_bus):
        """Failed commands should not emit events."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        await state_manager.start()
        await state_manager._handle_fan_mode_command(
            "casectl/fan/mode/set", b"invalid_mode"
        )
        await asyncio.sleep(0)

        assert len(received) == 0

    @pytest.mark.asyncio()
    async def test_no_event_bus(self, mock_mqtt, mock_config_manager):
        """Commands work without event bus (no event emitted)."""
        mgr = DeviceStateManager(mock_mqtt, config_manager=mock_config_manager)
        await mgr._handle_fan_mode_command("casectl/fan/mode/set", b"manual")
        assert mgr.command_count == 1
        # No crash — event emission is silently skipped

    @pytest.mark.asyncio()
    async def test_fan_channel_duty_emits_event(
        self, state_manager, mock_config_manager, event_bus
    ):
        """Per-channel fan duty command should emit event."""
        received = []
        event_bus.subscribe("mqtt_command_received", lambda data: received.append(data))

        mock_config_manager.get = AsyncMock(
            return_value={"manual_duty": [75, 75, 75]}
        )

        await state_manager.start()
        handler = state_manager._make_channel_duty_handler(1)
        await handler("casectl/fan/duty/1/set", b"50")
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["device"] == "fan"
        assert received[0]["action"] == "duty_channel_1"


# ---------------------------------------------------------------------------
# Subscribe command topics count test
# ---------------------------------------------------------------------------


class TestCommandTopicSubscription:
    """Tests for command topic subscription completeness."""

    @pytest.mark.asyncio()
    async def test_subscribes_to_12_command_topics(self, state_manager, mock_mqtt):
        """Should subscribe to 12 command topics including fan curve."""
        await state_manager.start()
        # 6 fan (mode, duty, duty/0, duty/1, duty/2, curve) +
        # 4 LED (mode, color, light, brightness) +
        # 2 OLED (rotation, screen) = 12
        assert mock_mqtt.subscribe.call_count == 12

    @pytest.mark.asyncio()
    async def test_fan_curve_topic_subscribed(self, state_manager, mock_mqtt):
        """The fan/curve/set topic should be subscribed."""
        await state_manager.start()
        subscribed_topics = [
            call.args[0] for call in mock_mqtt.subscribe.call_args_list
        ]
        assert "casectl/fan/curve/set" in subscribed_topics

    @pytest.mark.asyncio()
    async def test_unsubscribe_includes_fan_curve(self, state_manager, mock_mqtt):
        """Unsubscribe should include the fan/curve/set topic."""
        await state_manager.start()
        await state_manager.stop()
        unsubscribed_topics = [
            call.args[0] for call in mock_mqtt.unsubscribe.call_args_list
        ]
        assert "casectl/fan/curve/set" in unsubscribed_topics
