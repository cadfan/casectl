"""Tests for casectl.plugins.led.plugin.LedControlPlugin.

Exercises LED mode mapping, redundant I2C write avoidance, route/config
registration during setup, and graceful handling of missing hardware.
All hardware interactions are mocked -- no real I2C.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.config.models import LedConfig, LedMode
from casectl.hardware.expansion import LedHwMode
from casectl.plugins.base import HardwareRegistry, PluginContext, PluginStatus
from casectl.plugins.led.plugin import LedControlPlugin


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_expansion(connected: bool = True) -> MagicMock:
    """Create a mock ExpansionBoard with async I2C methods."""
    exp = MagicMock()
    exp.connected = connected
    exp.degraded = False
    exp.async_set_led_mode = AsyncMock()
    exp.async_set_all_led_color = AsyncMock()
    return exp


@pytest.fixture()
def mock_expansion() -> MagicMock:
    return _make_mock_expansion()


@pytest.fixture()
def plugin() -> LedControlPlugin:
    return LedControlPlugin()


@pytest.fixture()
def ctx(mock_expansion: MagicMock) -> PluginContext:
    """Build a PluginContext with mock config manager, event bus, and hardware."""
    config_mgr = AsyncMock()
    hw_registry = HardwareRegistry(
        expansion=mock_expansion, oled=None, system_info=None,
    )
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    return PluginContext(
        plugin_name="led-control",
        config_manager=config_mgr,
        hardware_registry=hw_registry,
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# Tests: setup
# ---------------------------------------------------------------------------


async def test_setup_registers_routes(plugin: LedControlPlugin, ctx: PluginContext) -> None:
    """After setup(), the context should have a router registered."""
    await plugin.setup(ctx)
    assert ctx.routes is not None


# ---------------------------------------------------------------------------
# Tests: LED mode mapping
# ---------------------------------------------------------------------------


async def test_mode_rainbow(plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock) -> None:
    """RAINBOW mode maps to LedHwMode.RAINBOW (=4)."""
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.RAINBOW)
    await plugin._apply_mode(config)

    mock_expansion.async_set_led_mode.assert_called_with(LedHwMode.RAINBOW)
    assert LedHwMode.RAINBOW == 4


async def test_mode_breathing(plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock) -> None:
    """BREATHING mode maps to LedHwMode.BREATHING (=3)."""
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.BREATHING)
    await plugin._apply_mode(config)

    mock_expansion.async_set_led_mode.assert_called_with(LedHwMode.BREATHING)
    assert LedHwMode.BREATHING == 3


async def test_mode_off(plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock) -> None:
    """OFF mode maps to LedHwMode.CLOSE (=0)."""
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.OFF)
    await plugin._apply_mode(config)

    mock_expansion.async_set_led_mode.assert_called_with(LedHwMode.CLOSE)
    assert LedHwMode.CLOSE == 0


async def test_mode_manual(plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock) -> None:
    """MANUAL mode maps to LedHwMode.RGB (=1) and writes colour."""
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.MANUAL, red_value=100, green_value=200, blue_value=50)
    await plugin._apply_mode(config)

    mock_expansion.async_set_led_mode.assert_called_with(LedHwMode.RGB)
    mock_expansion.async_set_all_led_color.assert_called_with(100, 200, 50)
    assert LedHwMode.RGB == 1


# ---------------------------------------------------------------------------
# Tests: redundant I2C write avoidance
# ---------------------------------------------------------------------------


async def test_apply_mode_skips_redundant_writes(
    plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock
) -> None:
    """Applying the same mode twice should not re-send the I2C command."""
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.RAINBOW)

    await plugin._apply_mode(config)
    assert mock_expansion.async_set_led_mode.call_count == 1

    # Apply same mode again
    await plugin._apply_mode(config)
    # Should still be 1 -- no second write
    assert mock_expansion.async_set_led_mode.call_count == 1


async def test_apply_manual_skips_redundant_color(
    plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock
) -> None:
    """In MANUAL mode, same colour is not re-sent on the bus."""
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.MANUAL, red_value=10, green_value=20, blue_value=30)

    await plugin._apply_mode(config)
    assert mock_expansion.async_set_all_led_color.call_count == 1

    # Apply same config again
    await plugin._apply_mode(config)
    assert mock_expansion.async_set_all_led_color.call_count == 1


async def test_apply_manual_updates_on_color_change(
    plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock
) -> None:
    """In MANUAL mode, a new colour triggers a write."""
    await plugin.setup(ctx)

    config1 = LedConfig(mode=LedMode.MANUAL, red_value=10, green_value=20, blue_value=30)
    await plugin._apply_mode(config1)
    assert mock_expansion.async_set_all_led_color.call_count == 1

    config2 = LedConfig(mode=LedMode.MANUAL, red_value=255, green_value=0, blue_value=0)
    await plugin._apply_mode(config2)
    assert mock_expansion.async_set_all_led_color.call_count == 2


# ---------------------------------------------------------------------------
# Tests: missing hardware
# ---------------------------------------------------------------------------


async def test_missing_expansion_graceful(plugin: LedControlPlugin) -> None:
    """Plugin handles expansion=None without raising."""
    config_mgr = AsyncMock()
    hw_registry = HardwareRegistry(expansion=None, oled=None, system_info=None)
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    ctx = PluginContext(
        plugin_name="led-control",
        config_manager=config_mgr,
        hardware_registry=hw_registry,
        event_bus=event_bus,
    )
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.RAINBOW)
    # Should not raise
    await plugin._apply_mode(config)


async def test_disconnected_expansion_sets_degraded(plugin: LedControlPlugin) -> None:
    """When expansion.connected is False, plugin sets degraded flag."""
    exp = _make_mock_expansion(connected=False)
    hw_registry = HardwareRegistry(expansion=exp, oled=None, system_info=None)
    config_mgr = AsyncMock()
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    ctx = PluginContext(
        plugin_name="led-control",
        config_manager=config_mgr,
        hardware_registry=hw_registry,
        event_bus=event_bus,
    )
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.RAINBOW)
    await plugin._apply_mode(config)

    assert plugin._degraded is True
    # No I2C writes should have been made
    exp.async_set_led_mode.assert_not_called()


async def test_get_status_returns_current_mode(plugin: LedControlPlugin, ctx: PluginContext, mock_expansion: MagicMock) -> None:
    """get_status() reflects the currently applied mode and colour."""
    await plugin.setup(ctx)
    config = LedConfig(mode=LedMode.MANUAL, red_value=11, green_value=22, blue_value=33)
    await plugin._apply_mode(config)

    status = plugin.get_status()
    assert status["mode"] == "manual"
    assert status["color"] == {"red": 11, "green": 22, "blue": 33}
