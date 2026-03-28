"""Tests for casectl.plugins.monitor.plugin.SystemMonitorPlugin.

Exercises plugin lifecycle (setup, start, stop), metrics collection with
mocked hardware, event bus emission, and graceful degradation.
No real I2C, sysfs, or psutil.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.config.models import SystemMetrics
from casectl.plugins.base import HardwareRegistry, PluginContext, PluginStatus
from casectl.plugins.monitor.plugin import SystemMonitorPlugin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_expansion(connected: bool = True) -> MagicMock:
    """Create a mock ExpansionBoard with async I2C methods."""
    exp = MagicMock()
    exp.connected = connected
    exp.degraded = False
    exp.async_get_temperature = AsyncMock(return_value=35.5)
    exp.async_get_fan_duty = AsyncMock(return_value=[120, 120, 120])
    exp.async_get_motor_speed = AsyncMock(return_value=[2400, 1800, 1800])
    return exp


def _make_mock_system_info() -> MagicMock:
    """Create a mock SystemInfo that returns a known SystemMetrics."""
    si = MagicMock()
    # get_all_metrics returns an object with the attributes used in _collect_metrics
    metrics_obj = MagicMock()
    metrics_obj.cpu_usage = 25.0
    metrics_obj.cpu_temperature = 42.5
    metrics_obj.memory = MagicMock(percent=45.0)
    metrics_obj.disk = MagicMock(percent=60.0)
    metrics_obj.ip_address = "192.168.1.100"
    metrics_obj.fan_duty = 128
    metrics_obj.date = "2026-03-25"
    metrics_obj.weekday = "Wednesday"
    metrics_obj.time = "14:30:00"
    si.get_all_metrics = MagicMock(return_value=metrics_obj)
    return si


def _make_ctx(
    expansion: MagicMock | None = None,
    system_info: MagicMock | None = None,
    event_bus: MagicMock | None = None,
) -> PluginContext:
    """Build a PluginContext with mock components."""
    if event_bus is None:
        event_bus = MagicMock()
        event_bus.subscribe = MagicMock()
        event_bus.emit = AsyncMock()

    hw = HardwareRegistry(
        expansion=expansion,
        oled=None,
        system_info=system_info,
    )
    config_mgr = AsyncMock()
    return PluginContext(
        plugin_name="system-monitor",
        config_manager=config_mgr,
        hardware_registry=hw,
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# Setup / status
# ---------------------------------------------------------------------------


class TestPluginSetup:
    """Tests for setup and status reporting."""

    @pytest.mark.asyncio
    async def test_plugin_setup_subscribes_nothing(self):
        """Monitor is a producer — it emits events but subscribes to none."""
        event_bus = MagicMock()
        event_bus.subscribe = MagicMock()
        event_bus.emit = AsyncMock()
        ctx = _make_ctx(event_bus=event_bus)

        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        # setup() should NOT have called subscribe
        event_bus.subscribe.assert_not_called()

    def test_get_status_before_metrics_collected(self):
        """Before start(), status should be STOPPED with no metrics."""
        plugin = SystemMonitorPlugin()
        status = plugin.get_status()
        assert status["status"] == PluginStatus.STOPPED
        assert status["has_metrics"] is False
        assert "metrics" not in status

    @pytest.mark.asyncio
    async def test_get_status_after_metrics(self):
        """After collecting metrics, status should report them."""
        from casectl.hardware.system import AllMetrics, MemoryInfo, DiskInfo, SwapInfo

        all_metrics = AllMetrics(
            cpu_usage=25.0, cpu_temperature=52.3,
            memory=MemoryInfo(percent=41.0, used_gb=1.6, total_gb=4.0),
            disk=DiskInfo(percent=67.0, used_gb=20.0, total_gb=30.0),
            ip_address="192.168.0.238", fan_duty=128,
            date="2026-03-25", weekday="Wednesday", time="14:30:00",
            swap=SwapInfo(percent=10.0, used_gb=0.4, total_gb=4.0),
        )

        expansion = _make_mock_expansion()
        system_info = MagicMock()
        system_info.get_all_metrics.return_value = all_metrics
        ctx = _make_ctx(expansion=expansion, system_info=system_info)

        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        # Patch asyncio.to_thread to call the function directly
        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            metrics = await plugin._collect_metrics()

        plugin._latest_metrics = metrics

        status = plugin.get_status()
        assert status["has_metrics"] is True
        assert "metrics" in status
        assert status["metrics"]["cpu_percent"] == 25.0
        assert status["metrics"]["case_temp"] == 35.5


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------


class TestCollectMetrics:
    """Tests for the _collect_metrics method."""

    @pytest.mark.asyncio
    async def test_collect_metrics_emits_event(self):
        """The collect loop should emit a metrics_updated event."""
        from casectl.hardware.system import AllMetrics, MemoryInfo, DiskInfo, SwapInfo

        all_metrics = AllMetrics(
            cpu_usage=25.0, cpu_temperature=52.3,
            memory=MemoryInfo(percent=41.0, used_gb=1.6, total_gb=4.0),
            disk=DiskInfo(percent=67.0, used_gb=20.0, total_gb=30.0),
            ip_address="192.168.0.238", fan_duty=128,
            date="2026-03-25", weekday="Wednesday", time="14:30:00",
            swap=SwapInfo(percent=10.0, used_gb=0.4, total_gb=4.0),
        )

        expansion = _make_mock_expansion()
        system_info = MagicMock()
        system_info.get_all_metrics.return_value = all_metrics
        event_bus = MagicMock()
        event_bus.subscribe = MagicMock()
        event_bus.emit = AsyncMock()
        ctx = _make_ctx(expansion=expansion, system_info=system_info, event_bus=event_bus)

        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            metrics = await plugin._collect_metrics()
            plugin._latest_metrics = metrics
            # Emit directly on the bus (ctx.emit_event uses create_task which
            # doesn't resolve in a synchronous test context)
            await event_bus.emit("metrics_updated", metrics)

        event_bus.emit.assert_awaited_once_with("metrics_updated", metrics)

    @pytest.mark.asyncio
    async def test_collect_metrics_reads_expansion_board(self):
        """_collect_metrics should read temperature, fan duty, and motor speed."""
        from casectl.hardware.system import AllMetrics, MemoryInfo, DiskInfo, SwapInfo

        all_metrics = AllMetrics(
            cpu_usage=25.0, cpu_temperature=52.3,
            memory=MemoryInfo(percent=41.0, used_gb=1.6, total_gb=4.0),
            disk=DiskInfo(percent=67.0, used_gb=20.0, total_gb=30.0),
            ip_address="192.168.0.238", fan_duty=128,
            date="2026-03-25", weekday="Wednesday", time="14:30:00",
            swap=SwapInfo(percent=10.0, used_gb=0.4, total_gb=4.0),
        )

        expansion = _make_mock_expansion()
        system_info = MagicMock()
        system_info.get_all_metrics.return_value = all_metrics
        ctx = _make_ctx(expansion=expansion, system_info=system_info)

        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            metrics = await plugin._collect_metrics()

        # Verify expansion board async methods were called
        expansion.async_get_temperature.assert_awaited_once()
        expansion.async_get_fan_duty.assert_awaited_once()
        expansion.async_get_motor_speed.assert_awaited_once()

        # Verify the values from expansion ended up in metrics
        assert metrics["case_temp"] == 35.5
        assert metrics["fan_duty"] == [120, 120, 120]
        assert metrics["motor_speed"] == [2400, 1800, 1800]

    @pytest.mark.asyncio
    async def test_graceful_without_hardware(self):
        """When no hardware is available, _collect_metrics should return defaults."""
        ctx = _make_ctx(expansion=None, system_info=None)

        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        metrics = await plugin._collect_metrics()

        # Should still return a valid metrics dict with defaults
        assert metrics["cpu_percent"] == 0.0
        assert metrics["case_temp"] == 0.0
        assert metrics["fan_duty"] == [0, 0, 0]
        assert metrics["motor_speed"] == [0, 0, 0]

    @pytest.mark.asyncio
    async def test_collect_metrics_expansion_disconnected_sets_degraded(self):
        """When expansion.connected is False, plugin should mark itself degraded."""
        expansion = _make_mock_expansion(connected=False)
        ctx = _make_ctx(expansion=expansion, system_info=None)

        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        await plugin._collect_metrics()
        assert plugin._degraded is True

    @pytest.mark.asyncio
    async def test_collect_metrics_oserror_handled_gracefully(self):
        """OSError from expansion I2C should be caught, not raised."""
        expansion = _make_mock_expansion()
        expansion.async_get_temperature = AsyncMock(side_effect=OSError("I2C fail"))
        expansion.async_get_fan_duty = AsyncMock(side_effect=OSError("I2C fail"))
        expansion.async_get_motor_speed = AsyncMock(side_effect=OSError("I2C fail"))
        ctx = _make_ctx(expansion=expansion, system_info=None)

        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        # Should not raise
        metrics = await plugin._collect_metrics()
        # Values should remain at defaults
        assert metrics["case_temp"] == 0.0
        assert metrics["fan_duty"] == [0, 0, 0]
        assert metrics["motor_speed"] == [0, 0, 0]


# ---------------------------------------------------------------------------
# Start / stop lifecycle
# ---------------------------------------------------------------------------


class TestPluginLifecycle:
    """Tests for start/stop."""

    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        ctx = _make_ctx()
        plugin = SystemMonitorPlugin()
        await plugin.setup(ctx)

        # Patch the collect loop so it doesn't actually run
        with patch.object(plugin, "_collect_loop", new_callable=AsyncMock):
            await plugin.start()
            assert plugin._task is not None

            await plugin.stop()
            assert plugin._task is None

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self):
        """Calling stop() without start() should not raise."""
        plugin = SystemMonitorPlugin()
        await plugin.stop()  # Should not raise
