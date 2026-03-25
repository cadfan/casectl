"""Tests for casectl.plugins.prometheus (plugin + routes).

Validates Prometheus text exposition format, metric naming, HELP/TYPE lines,
channel labels for fan metrics, event-driven metric updates, and empty-metric
output.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from casectl.plugins.base import HardwareRegistry, PluginContext, PluginStatus
from casectl.plugins.prometheus.plugin import PrometheusPlugin
from casectl.plugins.prometheus.routes import _build_metrics_text, _format_gauge, configure


# ---------------------------------------------------------------------------
# Sample metrics data
# ---------------------------------------------------------------------------

SAMPLE_METRICS: dict[str, Any] = {
    "cpu_temp": 55.3,
    "case_temp": 32.1,
    "cpu_percent": 42.0,
    "memory_percent": 67.5,
    "disk_percent": 38.0,
    "fan_duty": [128, 200, 50],
    "motor_speed": [1200, 1500, 900],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def plugin() -> PrometheusPlugin:
    return PrometheusPlugin()


@pytest.fixture()
def ctx() -> PluginContext:
    config_mgr = AsyncMock()
    hw_registry = HardwareRegistry(expansion=None, oled=None, system_info=None)
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    return PluginContext(
        plugin_name="prometheus",
        config_manager=config_mgr,
        hardware_registry=hw_registry,
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# Tests: Prometheus text format
# ---------------------------------------------------------------------------


def test_build_metrics_text_contains_expected_metric_names() -> None:
    """Output contains all expected Prometheus metric names."""
    text = _build_metrics_text(SAMPLE_METRICS)

    expected_names = [
        "casectl_cpu_temp_celsius",
        "casectl_case_temp_celsius",
        "casectl_cpu_usage_ratio",
        "casectl_memory_usage_ratio",
        "casectl_disk_usage_ratio",
        "casectl_fan_duty_ratio",
        "casectl_fan_rpm",
    ]
    for name in expected_names:
        assert name in text, f"Expected metric {name!r} not found in output"


def test_build_metrics_text_has_help_and_type_lines() -> None:
    """Each metric family has HELP and TYPE lines."""
    text = _build_metrics_text(SAMPLE_METRICS)

    expected_families = [
        "casectl_cpu_temp_celsius",
        "casectl_case_temp_celsius",
        "casectl_cpu_usage_ratio",
        "casectl_memory_usage_ratio",
        "casectl_disk_usage_ratio",
        "casectl_fan_duty_ratio",
        "casectl_fan_rpm",
    ]
    for family in expected_families:
        assert f"# HELP {family}" in text, f"Missing HELP for {family}"
        assert f"# TYPE {family} gauge" in text, f"Missing TYPE for {family}"


def test_fan_duty_channel_labels() -> None:
    """Fan duty metrics have {channel="0"}, {channel="1"}, {channel="2"} labels."""
    text = _build_metrics_text(SAMPLE_METRICS)

    for ch in ["0", "1", "2"]:
        assert f'casectl_fan_duty_ratio{{channel="{ch}"}}' in text, \
            f'Missing channel="{ch}" label on fan duty metric'


def test_fan_rpm_channel_labels() -> None:
    """Fan RPM metrics have channel labels for all three channels."""
    text = _build_metrics_text(SAMPLE_METRICS)

    for ch in ["0", "1", "2"]:
        assert f'casectl_fan_rpm{{channel="{ch}"}}' in text, \
            f'Missing channel="{ch}" label on fan RPM metric'


def test_metric_values_correct() -> None:
    """Metric values are correctly computed from the input."""
    text = _build_metrics_text(SAMPLE_METRICS)

    # CPU temp should be a raw value
    assert "casectl_cpu_temp_celsius 55.3" in text

    # CPU usage: 42.0% -> 0.42 ratio
    assert "casectl_cpu_usage_ratio 0.42" in text

    # Memory usage: 67.5% -> 0.675 ratio
    assert "casectl_memory_usage_ratio 0.675" in text


def test_empty_metrics_returns_valid_output() -> None:
    """Empty metrics dict produces valid zero-value Prometheus output."""
    text = _build_metrics_text({})

    # Should still have metric names with zero values
    assert "casectl_cpu_temp_celsius 0.0" in text
    assert "casectl_cpu_usage_ratio 0.0" in text
    assert "casectl_memory_usage_ratio 0.0" in text
    assert "casectl_disk_usage_ratio 0.0" in text

    # Fan duty should default to zero
    for ch in ["0", "1", "2"]:
        assert f'casectl_fan_duty_ratio{{channel="{ch}"}} 0.0' in text


# ---------------------------------------------------------------------------
# Tests: Plugin lifecycle and event handling
# ---------------------------------------------------------------------------


async def test_plugin_setup_registers_routes(plugin: PrometheusPlugin, ctx: PluginContext) -> None:
    """setup() registers a FastAPI router on the context."""
    await plugin.setup(ctx)
    assert ctx.routes is not None


async def test_plugin_setup_subscribes_to_metrics_event(
    plugin: PrometheusPlugin, ctx: PluginContext
) -> None:
    """setup() subscribes to metrics_updated event on the event bus."""
    await plugin.setup(ctx)
    ctx._event_bus.subscribe.assert_called_once_with(
        "metrics_updated", plugin._on_metrics_updated,
    )


async def test_metrics_updated_event_caches_data(plugin: PrometheusPlugin, ctx: PluginContext) -> None:
    """Receiving a metrics_updated event caches the latest metrics."""
    await plugin.setup(ctx)

    assert plugin._latest_metrics is None

    await plugin._on_metrics_updated(SAMPLE_METRICS)

    assert plugin._latest_metrics is not None
    assert plugin._latest_metrics["cpu_temp"] == 55.3


def test_format_gauge_with_labels() -> None:
    """_format_gauge produces correct output with labels."""
    result = _format_gauge("test_metric", "A test.", 42.5, {"host": "pi"})
    assert '# HELP test_metric A test.' in result
    assert '# TYPE test_metric gauge' in result
    assert 'test_metric{host="pi"} 42.5' in result


def test_format_gauge_without_labels() -> None:
    """_format_gauge produces correct output without labels."""
    result = _format_gauge("test_metric", "A test.", 0.0)
    assert "test_metric 0.0" in result
