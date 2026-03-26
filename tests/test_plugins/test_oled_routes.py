"""Tests for casectl.plugins.oled.routes — FastAPI OLED display endpoints.

Exercises GET /status, POST /screen, and POST /rotation via a TestClient
with mocked status and config dependencies.  No real hardware.
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
    """Create a TestClient with mocked OLED route dependencies."""
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
            {"enabled": True},
            {"enabled": True},
            {"enabled": False},
            {"enabled": True},
        ],
        "rotation": 180,
    })
    config_manager.update = AsyncMock()

    get_config = MagicMock(return_value=config_manager)

    routes.configure(get_status=get_status, get_config=get_config)

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    return client, get_status, config_manager, get_config


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
        """If configure() was never called, _get_status is None -> 503."""
        routes._get_status = None
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/status")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /screen
# ---------------------------------------------------------------------------


class TestSetScreen:
    """Tests for the POST /screen endpoint."""

    def test_set_screen_enable(self):
        client, _, config_manager, get_config = _make_client()
        # Re-post to ensure fresh config state
        resp = client.post("/screen", json={"index": 2, "enabled": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["index"] == 2
        assert data["enabled"] is True
        get_config.assert_called()
        config_manager.get.assert_awaited_once_with("oled")
        config_manager.update.assert_awaited_once()

    def test_set_screen_disable(self):
        client, _, config_manager, _ = _make_client()
        resp = client.post("/screen", json={"index": 0, "enabled": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["index"] == 0
        assert data["enabled"] is False

    def test_set_screen_index_out_of_range_returns_400(self):
        """Screen index beyond the number of screens -> 400."""
        client, _, config_manager, _ = _make_client()
        # The mock config has 4 screens (indices 0-3).
        # Setting index=3 is valid (ge=0, le=3), but using an index
        # that equals len(screens) triggers the route's own 400 check.
        # We need to set up a config with fewer screens to trigger the route's check.
        config_manager.get = AsyncMock(return_value={
            "screens": [{"enabled": True}],  # only 1 screen
        })
        resp = client.post("/screen", json={"index": 2, "enabled": True})
        assert resp.status_code == 400

    def test_set_screen_index_negative_returns_422(self):
        """Negative index violates ge=0 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.post("/screen", json={"index": -1, "enabled": True})
        assert resp.status_code == 422

    def test_set_screen_index_too_high_returns_422(self):
        """Index > 3 violates le=3 -> Pydantic 422."""
        client, *_ = _make_client()
        resp = client.post("/screen", json={"index": 4, "enabled": True})
        assert resp.status_code == 422

    def test_set_screen_503_when_not_configured(self):
        """If _get_config is None, we get a 503."""
        routes._get_config = None
        # _get_status must still be set for the module-level state
        routes._get_status = MagicMock(return_value={})
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/screen", json={"index": 0, "enabled": True})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /rotation
# ---------------------------------------------------------------------------


class TestSetRotation:
    """Tests for the POST /rotation endpoint."""

    def test_set_rotation_valid_0(self):
        client, _, config_manager, get_config = _make_client()
        resp = client.post("/rotation", json={"rotation": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["rotation"] == 0
        get_config.assert_called()
        config_manager.update.assert_awaited_once_with("oled", {"rotation": 0})

    def test_set_rotation_valid_180(self):
        client, _, config_manager, _ = _make_client()
        resp = client.post("/rotation", json={"rotation": 180})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["rotation"] == 180

    def test_set_rotation_invalid_returns_422(self):
        """Values other than 0 or 180 should be rejected by Literal constraint."""
        client, *_ = _make_client()
        resp = client.post("/rotation", json={"rotation": 90})
        assert resp.status_code == 422

        resp = client.post("/rotation", json={"rotation": 270})
        assert resp.status_code == 422

        resp = client.post("/rotation", json={"rotation": -1})
        assert resp.status_code == 422

    def test_set_rotation_503_when_not_configured(self):
        """If _get_config is None, we get a 503."""
        routes._get_config = None
        routes._get_status = MagicMock(return_value={})
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/rotation", json={"rotation": 0})
        assert resp.status_code == 503
