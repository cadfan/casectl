"""Tests for casectl.plugins.oled.routes -- FastAPI OLED display endpoints.

Exercises GET /status, PUT /screen, PUT /rotation, PUT /power, and
PUT /content via a TestClient with mocked status and config dependencies
injected via app.state.  No real hardware.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casectl.plugins.oled import routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client():
    """Create a TestClient with mocked OLED route dependencies via app.state."""
    status_data = {
        "current_screen": 1,
        "screen_names": ["System", "Network", "Fan", "Custom"],
        "screens_enabled": [True, True, False, True],
        "rotation": 180,
        "degraded": False,
    }
    get_status = MagicMock(return_value=status_data)

    config_manager = AsyncMock()
    config_manager.get = AsyncMock(return_value={
        "screens": [
            {"enabled": True, "display_time": 5.0, "time_format": 0, "date_format": 0, "interchange": 0},
            {"enabled": True, "display_time": 5.0, "time_format": 0, "date_format": 0, "interchange": 0},
            {"enabled": False, "display_time": 5.0, "time_format": 0, "date_format": 0, "interchange": 0},
            {"enabled": True, "display_time": 5.0, "time_format": 0, "date_format": 0, "interchange": 0},
        ],
        "rotation": 180,
    })
    config_manager.update = AsyncMock()

    app = FastAPI()
    app.state.oled_get_status = get_status
    app.state.oled_config_manager = config_manager
    app.include_router(routes.router)
    client = TestClient(app)
    return client, get_status, config_manager


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


class TestOledStatus:
    """Tests for the GET /status endpoint."""

    def test_oled_status_returns_screens(self):
        client, get_status, *_ = _make_client()
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_screen"] == 1
        assert data["screen_names"] == ["System", "Network", "Fan", "Custom"]
        assert data["screens_enabled"] == [True, True, False, True]
        assert data["rotation"] == 180
        assert data["degraded"] is False
        get_status.assert_called_once()

    def test_oled_status_503_when_not_configured(self):
        """If oled_get_status is not set on app.state -> 503."""
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/status")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# PUT /screen
# ---------------------------------------------------------------------------


class TestSetScreen:
    """Tests for the PUT /screen endpoint."""

    def test_set_screen_enable(self):
        client, _, config_manager = _make_client()
        resp = client.put("/screen", json={"index": 2, "enabled": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["index"] == 2
        assert data["enabled"] is True
        config_manager.get.assert_awaited_once_with("oled")
        config_manager.update.assert_awaited_once()

    def test_set_screen_disable(self):
        client, _, config_manager = _make_client()
        resp = client.put("/screen", json={"index": 0, "enabled": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["index"] == 0
        assert data["enabled"] is False

    def test_set_screen_index_out_of_range_returns_400(self):
        """Screen index beyond the number of screens -> 400."""
        client, _, config_manager = _make_client()
        # The mock config has 4 screens (indices 0-3).
        # Setting index=3 is valid (ge=0, le=3), but using an index
        # that equals len(screens) triggers the route's own 400 check.
        # We need to set up a config with fewer screens to trigger the route's check.
        config_manager.get = AsyncMock(return_value={
            "screens": [{"enabled": True}],  # only 1 screen
        })
        resp = client.put("/screen", json={"index": 2, "enabled": True})
        assert resp.status_code == 400

    def test_set_screen_index_negative_returns_422(self):
        """Negative index violates ge=0 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/screen", json={"index": -1, "enabled": True})
        assert resp.status_code == 422

    def test_set_screen_index_too_high_returns_422(self):
        """Index > 3 violates le=3 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/screen", json={"index": 4, "enabled": True})
        assert resp.status_code == 422

    def test_set_screen_503_when_not_configured(self):
        """If oled_config_manager is not set on app.state, we get a 503."""
        app = FastAPI()
        # Set oled_get_status but NOT oled_config_manager
        app.state.oled_get_status = MagicMock(return_value={})
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/screen", json={"index": 0, "enabled": True})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# PUT /rotation
# ---------------------------------------------------------------------------


class TestSetRotation:
    """Tests for the PUT /rotation endpoint."""

    def test_set_rotation_valid_0(self):
        client, _, config_manager = _make_client()
        resp = client.put("/rotation", json={"rotation": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["rotation"] == 0
        config_manager.update.assert_awaited_once_with("oled", {"rotation": 0})

    def test_set_rotation_valid_180(self):
        client, _, config_manager = _make_client()
        resp = client.put("/rotation", json={"rotation": 180})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["rotation"] == 180

    def test_set_rotation_invalid_returns_422(self):
        """Values other than 0 or 180 should be rejected by Literal constraint."""
        client, *_ = _make_client()
        resp = client.put("/rotation", json={"rotation": 90})
        assert resp.status_code == 422

        resp = client.put("/rotation", json={"rotation": 270})
        assert resp.status_code == 422

        resp = client.put("/rotation", json={"rotation": -1})
        assert resp.status_code == 422

    def test_set_rotation_503_when_not_configured(self):
        """If oled_config_manager is not set on app.state, we get a 503."""
        app = FastAPI()
        app.state.oled_get_status = MagicMock(return_value={})
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/rotation", json={"rotation": 0})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# PUT /power
# ---------------------------------------------------------------------------


class TestSetPower:
    """Tests for the PUT /power endpoint (enable/disable all screens)."""

    def test_power_on_enables_all_screens(self):
        client, _, config_manager = _make_client()
        resp = client.put("/power", json={"enabled": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["enabled"] is True
        config_manager.get.assert_awaited_once_with("oled")
        config_manager.update.assert_awaited_once()
        # Verify all screens were set to enabled
        call_args = config_manager.update.call_args
        screens = call_args[0][1]["screens"]
        assert all(s["enabled"] is True for s in screens)

    def test_power_off_disables_all_screens(self):
        client, _, config_manager = _make_client()
        resp = client.put("/power", json={"enabled": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["enabled"] is False
        call_args = config_manager.update.call_args
        screens = call_args[0][1]["screens"]
        assert all(s["enabled"] is False for s in screens)

    def test_power_missing_enabled_returns_422(self):
        """Missing 'enabled' field -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/power", json={})
        assert resp.status_code == 422

    def test_power_invalid_enabled_returns_422(self):
        """Non-boolean 'enabled' value -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/power", json={"enabled": 123})
        assert resp.status_code == 422

    def test_power_503_when_not_configured(self):
        """If oled_config_manager is not set on app.state -> 503."""
        app = FastAPI()
        app.state.oled_get_status = MagicMock(return_value={})
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/power", json={"enabled": True})
        assert resp.status_code == 503

    def test_power_handles_config_error(self):
        """Internal errors -> 500."""
        client, _, config_manager = _make_client()
        config_manager.get = AsyncMock(side_effect=RuntimeError("boom"))
        resp = client.put("/power", json={"enabled": True})
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# PUT /content
# ---------------------------------------------------------------------------


class TestSetContent:
    """Tests for the PUT /content endpoint (per-screen content settings)."""

    def test_set_display_time(self):
        client, _, config_manager = _make_client()
        resp = client.put("/content", json={
            "screen_index": 0,
            "display_time": 10.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["screen_index"] == 0
        assert data["settings"]["display_time"] == 10.0
        config_manager.update.assert_awaited_once()

    def test_set_time_format(self):
        client, _, config_manager = _make_client()
        resp = client.put("/content", json={
            "screen_index": 0,
            "time_format": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["settings"]["time_format"] == 1

    def test_set_date_format(self):
        client, _, config_manager = _make_client()
        resp = client.put("/content", json={
            "screen_index": 1,
            "date_format": 2,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["date_format"] == 2

    def test_set_interchange(self):
        client, _, config_manager = _make_client()
        resp = client.put("/content", json={
            "screen_index": 2,
            "interchange": 3,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["interchange"] == 3

    def test_set_multiple_content_fields(self):
        """Can update multiple content fields in one request."""
        client, _, config_manager = _make_client()
        resp = client.put("/content", json={
            "screen_index": 0,
            "display_time": 8.0,
            "time_format": 1,
            "date_format": 1,
            "interchange": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["display_time"] == 8.0
        assert data["settings"]["time_format"] == 1
        assert data["settings"]["date_format"] == 1
        assert data["settings"]["interchange"] == 1

    def test_set_content_no_optional_fields(self):
        """If no optional content fields are provided, update still succeeds (no-op update)."""
        client, _, config_manager = _make_client()
        resp = client.put("/content", json={"screen_index": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_set_content_screen_index_out_of_range(self):
        """Screen index beyond config screens -> 400."""
        client, _, config_manager = _make_client()
        config_manager.get = AsyncMock(return_value={
            "screens": [{"enabled": True, "display_time": 5.0}],
        })
        resp = client.put("/content", json={
            "screen_index": 2,
            "display_time": 10.0,
        })
        assert resp.status_code == 400

    def test_set_content_negative_index_returns_422(self):
        """Negative index violates ge=0 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/content", json={"screen_index": -1})
        assert resp.status_code == 422

    def test_set_content_index_too_high_returns_422(self):
        """Index > 3 violates le=3 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/content", json={"screen_index": 5})
        assert resp.status_code == 422

    def test_set_content_negative_display_time_returns_422(self):
        """Negative display_time violates gt=0 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/content", json={
            "screen_index": 0, "display_time": -1.0,
        })
        assert resp.status_code == 422

    def test_set_content_zero_display_time_returns_422(self):
        """Zero display_time violates gt=0 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/content", json={
            "screen_index": 0, "display_time": 0,
        })
        assert resp.status_code == 422

    def test_set_content_invalid_time_format_returns_422(self):
        """time_format not 0 or 1 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.put("/content", json={
            "screen_index": 0, "time_format": 2,
        })
        assert resp.status_code == 422

    def test_set_content_503_when_not_configured(self):
        """If oled_config_manager is not set on app.state -> 503."""
        app = FastAPI()
        app.state.oled_get_status = MagicMock(return_value={})
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/content", json={"screen_index": 0})
        assert resp.status_code == 503

    def test_set_content_handles_config_error(self):
        """Internal errors -> 500."""
        client, _, config_manager = _make_client()
        config_manager.get = AsyncMock(side_effect=RuntimeError("boom"))
        resp = client.put("/content", json={
            "screen_index": 0, "display_time": 5.0,
        })
        assert resp.status_code == 500
