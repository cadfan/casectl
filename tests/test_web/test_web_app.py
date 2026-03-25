"""Tests for casectl.web.app (web dashboard router).

Uses FastAPI TestClient with mocked PluginHost and ConfigManager to exercise
the dashboard page and HTMX partial routes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casectl.plugins.base import HardwareRegistry, PluginStatus
from casectl.web.app import create_web_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_plugin(status_data: dict[str, Any]) -> MagicMock:
    """Create a mock plugin with a get_status() that returns *status_data*."""
    plugin = MagicMock()
    plugin.get_status.return_value = status_data
    return plugin


def _make_test_client() -> tuple[TestClient, MagicMock, MagicMock]:
    """Build a FastAPI app containing only the web dashboard router.

    Returns (TestClient, mock_plugin_host, mock_config_manager).
    """
    plugin_host = MagicMock()
    config_manager = MagicMock()

    # system-monitor plugin provides metrics
    monitor_plugin = _make_mock_plugin({
        "metrics": {
            "cpu_temp": 48.5,
            "cpu_percent": 32.0,
            "memory_percent": 55.0,
            "disk_percent": 40.0,
            "ip_address": "192.168.1.100",
            "case_temp": 30.0,
            "motor_speed": [1000, 1100, 900],
        },
    })

    # fan-control plugin provides fan status
    fan_plugin = _make_mock_plugin({
        "mode": "follow_temp",
        "duty": [128, 128, 128],
        "degraded": False,
    })

    # led-control plugin provides LED status
    led_plugin = _make_mock_plugin({
        "mode": "rainbow",
        "color": {"red": 0, "green": 0, "blue": 255},
        "degraded": False,
    })

    # oled-display plugin provides OLED status
    oled_plugin = _make_mock_plugin({
        "current_screen": 0,
        "screen_names": ["clock", "metrics", "temperature", "fan_duty"],
        "screens_enabled": [True, True, True, True],
        "rotation": 180,
        "degraded": False,
    })

    def get_plugin(name: str) -> MagicMock | None:
        return {
            "system-monitor": monitor_plugin,
            "fan-control": fan_plugin,
            "led-control": led_plugin,
            "oled-display": oled_plugin,
        }.get(name)

    plugin_host.get_plugin.side_effect = get_plugin
    plugin_host.get_all_statuses.return_value = {
        "system-monitor": PluginStatus.HEALTHY,
        "fan-control": PluginStatus.HEALTHY,
        "led-control": PluginStatus.HEALTHY,
        "oled-display": PluginStatus.HEALTHY,
    }

    router = create_web_router(plugin_host, config_manager)

    app = FastAPI()
    app.include_router(router)

    client = TestClient(app, raise_server_exceptions=False)
    return client, plugin_host, config_manager


# ---------------------------------------------------------------------------
# Tests: Main dashboard
# ---------------------------------------------------------------------------


def test_dashboard_returns_200_with_casectl() -> None:
    """GET / returns 200 with HTML containing 'casectl'."""
    client, _, _ = _make_test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "casectl" in resp.text.lower()


def test_dashboard_contains_cpu_temp() -> None:
    """GET / renders the CPU temperature value."""
    client, _, _ = _make_test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "48.5" in resp.text


# ---------------------------------------------------------------------------
# Tests: HTMX partials
# ---------------------------------------------------------------------------


def test_partial_monitor_returns_html_with_cpu() -> None:
    """GET /w/monitor returns HTML partial with CPU data."""
    client, _, _ = _make_test_client()
    resp = client.get("/w/monitor")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Should contain the CPU temperature
    assert "48.5" in resp.text


def test_partial_fan_returns_html_with_fan_data() -> None:
    """GET /w/fan returns HTML partial with fan data."""
    client, _, _ = _make_test_client()
    resp = client.get("/w/fan")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Should contain the fan mode
    assert "Fan" in resp.text or "fan" in resp.text


def test_partial_led_returns_html_with_led_data() -> None:
    """GET /w/led returns HTML partial with LED data."""
    client, _, _ = _make_test_client()
    resp = client.get("/w/led")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Should contain mode info
    assert "rainbow" in resp.text.lower() or "Rainbow" in resp.text


def test_partial_oled_returns_html_with_oled_data() -> None:
    """GET /w/oled returns HTML partial with OLED data."""
    client, _, _ = _make_test_client()
    resp = client.get("/w/oled")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Should contain OLED-related content (preview box or screen controls)
    assert "oled" in resp.text.lower()


# ---------------------------------------------------------------------------
# Tests: Missing plugins fallback
# ---------------------------------------------------------------------------


def test_dashboard_with_no_plugins_returns_200() -> None:
    """Dashboard renders successfully even when all plugins return None."""
    plugin_host = MagicMock()
    config_manager = MagicMock()
    plugin_host.get_plugin.return_value = None
    plugin_host.get_all_statuses.return_value = {}

    router = create_web_router(plugin_host, config_manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "casectl" in resp.text.lower()


def test_partial_monitor_with_no_plugin() -> None:
    """GET /w/monitor still returns 200 when system-monitor plugin is None."""
    plugin_host = MagicMock()
    config_manager = MagicMock()
    plugin_host.get_plugin.return_value = None
    plugin_host.get_all_statuses.return_value = {}

    router = create_web_router(plugin_host, config_manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/w/monitor")
    assert resp.status_code == 200


def test_partial_fan_with_no_plugin() -> None:
    """GET /w/fan still returns 200 when fan-control plugin is None."""
    plugin_host = MagicMock()
    config_manager = MagicMock()
    plugin_host.get_plugin.return_value = None
    plugin_host.get_all_statuses.return_value = {}

    router = create_web_router(plugin_host, config_manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/w/fan")
    assert resp.status_code == 200
