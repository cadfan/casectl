"""Tests for casectl.plugins.fan.routes — FastAPI fan-control endpoints.

Exercises GET /status, PUT /mode, and PUT /speed via a TestClient with
mocked controller and config dependencies injected through app.state.
No real hardware or I2C.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casectl.config.models import FanMode
from casectl.plugins.fan import routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client():
    """Create a TestClient with mocked fan route dependencies via app.state."""
    controller = MagicMock()
    controller.current_mode = FanMode.FOLLOW_TEMP
    controller.current_duty = [75, 75, 75]
    controller.degraded = False
    controller.get_motor_speeds = AsyncMock(return_value=[2400, 1800, 1800])
    controller.get_cpu_temperature = AsyncMock(return_value=52.3)

    config_manager = AsyncMock()
    config_manager.update = AsyncMock()

    app = FastAPI()
    app.state.fan_controller = controller
    app.state.fan_config_manager = config_manager
    app.include_router(routes.router)
    client = TestClient(app)
    return client, controller, config_manager


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


class TestFanStatus:
    """Tests for the GET /status endpoint."""

    def test_fan_status_returns_mode_and_duty(self):
        client, controller, _ = _make_client()
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "follow_temp"
        assert data["duty"] == [75, 75, 75]
        assert data["degraded"] is False

    def test_fan_status_includes_rpm(self):
        client, controller, _ = _make_client()
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["rpm"] == [2400, 1800, 1800]
        assert data["temp"] == 52.3
        controller.get_motor_speeds.assert_called_once()
        controller.get_cpu_temperature.assert_called_once()

    def test_fan_status_503_when_not_configured(self):
        """If app.state has no fan_controller, we get a 503."""
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/status")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# PUT /mode
# ---------------------------------------------------------------------------


class TestSetFanMode:
    """Tests for the PUT /mode endpoint."""

    def test_set_fan_mode_valid(self):
        client, _, config_manager = _make_client()
        resp = client.put("/mode", json={"mode": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "manual"
        config_manager.update.assert_awaited_once_with("fan", {"mode": 2})

    def test_set_fan_mode_all_valid_values(self):
        """All FanMode enum values (0-4) should be accepted."""
        for mode_val in range(5):
            client, _, config_manager = _make_client()
            resp = client.put("/mode", json={"mode": mode_val})
            assert resp.status_code == 200, f"mode={mode_val} should be valid"

    def test_set_fan_mode_invalid_returns_422(self):
        """Invalid mode values should be rejected by Pydantic validator."""
        client, *_ = _make_client()
        resp = client.put("/mode", json={"mode": 99})
        assert resp.status_code == 422  # integer out of range

        resp = client.put("/mode", json={"mode": "turbo"})
        assert resp.status_code == 422  # unknown string name

    def test_set_fan_mode_503_when_not_configured(self):
        """If app.state has no fan_config_manager, we get a 503."""
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/mode", json={"mode": 0})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# PUT /speed
# ---------------------------------------------------------------------------


class TestSetFanSpeed:
    """Tests for the PUT /speed endpoint."""

    def test_set_fan_speed_valid(self):
        client, _, config_manager = _make_client()
        resp = client.put("/speed", json={"duty": [50, 60, 70]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        # 50 * 255 / 100 = 127, 60 * 255 / 100 = 153, 70 * 255 / 100 = 178
        assert data["duty_hw"] == [127, 153, 178]
        config_manager.update.assert_awaited_once_with("fan", {
            "mode": FanMode.MANUAL.value,
            "manual_duty": [127, 153, 178],
        })

    def test_set_fan_speed_pads_to_three_channels(self):
        """If fewer than 3 channels supplied, should pad with last value."""
        client, _, config_manager = _make_client()
        resp = client.put("/speed", json={"duty": [100]})
        assert resp.status_code == 200
        data = resp.json()
        # 100 * 255 / 100 = 255, padded to 3 channels
        assert data["duty_hw"] == [255, 255, 255]

    def test_set_fan_speed_out_of_range_returns_422(self):
        """Duty values outside 0-100 should be rejected by Pydantic."""
        client, *_ = _make_client()
        resp = client.put("/speed", json={"duty": [101]})
        assert resp.status_code == 422

        resp = client.put("/speed", json={"duty": [-1]})
        assert resp.status_code == 422

    def test_set_fan_speed_empty_list_returns_422(self):
        """Empty duty list should fail min_length=1 validation."""
        client, *_ = _make_client()
        resp = client.put("/speed", json={"duty": []})
        assert resp.status_code == 422

    def test_set_fan_speed_too_many_channels_returns_422(self):
        """More than 3 duty values should fail max_length=3 validation."""
        client, *_ = _make_client()
        resp = client.put("/speed", json={"duty": [50, 50, 50, 50]})
        assert resp.status_code == 422

    def test_set_fan_speed_503_when_not_configured(self):
        """If app.state has no fan_config_manager, we get a 503."""
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/speed", json={"duty": [50]})
        assert resp.status_code == 503
