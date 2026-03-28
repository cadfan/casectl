"""Tests for the MQTT plugin REST API routes.

Uses FastAPI's test client to exercise the route handlers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casectl.plugins.mqtt.routes import router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_mqtt_manager() -> MagicMock:
    """Return a mock MqttConnectionManager."""
    mgr = MagicMock()
    mgr.get_status.return_value = {
        "state": "connected",
        "broker": "localhost",
        "port": 1883,
        "client_id": "casectl",
        "reconnect_count": 0,
        "subscriptions": ["casectl/fan/mode/set"],
    }
    return mgr


@pytest.fixture()
def mock_state_manager() -> MagicMock:
    """Return a mock DeviceStateManager."""
    mgr = MagicMock()
    mgr.get_status.return_value = {
        "subscribed": True,
        "publish_count": 42,
        "command_count": 5,
        "error_count": 1,
        "topic_prefix": "casectl",
    }
    return mgr


@pytest.fixture()
def mock_metric_publisher() -> MagicMock:
    """Return a mock MetricPublisher."""
    pub = MagicMock()
    pub.get_status.return_value = {
        "running": True,
        "publish_count": 100,
        "error_count": 0,
        "publish_interval": 10.0,
        "topic_prefix": "casectl",
        "has_cached_metrics": True,
    }
    return pub


@pytest.fixture()
def mock_ha_discovery() -> MagicMock:
    """Return a mock HADiscoveryManager."""
    disc = MagicMock()
    disc.get_status.return_value = {
        "published": True,
        "entity_count": 15,
        "publish_count": 1,
        "ha_discovery_prefix": "homeassistant",
        "device_id": "casectl",
    }
    return disc


@pytest.fixture()
def app_with_mqtt(
    mock_mqtt_manager,
    mock_state_manager,
    mock_metric_publisher,
    mock_ha_discovery,
) -> FastAPI:
    """Create a FastAPI app with all MQTT components on app.state."""
    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/mqtt")
    app.state.mqtt_manager = mock_mqtt_manager
    app.state.mqtt_state_manager = mock_state_manager
    app.state.mqtt_metric_publisher = mock_metric_publisher
    app.state.mqtt_ha_discovery = mock_ha_discovery
    return app


@pytest.fixture()
def client(app_with_mqtt) -> TestClient:
    """Return a TestClient for the MQTT routes."""
    return TestClient(app_with_mqtt)


@pytest.fixture()
def empty_app() -> FastAPI:
    """Create a FastAPI app without any MQTT state."""
    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/mqtt")
    return app


@pytest.fixture()
def empty_client(empty_app) -> TestClient:
    """Return a TestClient for an app with no MQTT state."""
    return TestClient(empty_app)


# ---------------------------------------------------------------------------
# /status endpoint tests
# ---------------------------------------------------------------------------


class TestMqttStatusEndpoint:
    """Tests for GET /api/plugins/mqtt/status."""

    def test_returns_combined_status(self, client):
        resp = client.get("/api/plugins/mqtt/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "connection" in data
        assert "state_manager" in data
        assert "metric_publisher" in data
        assert "ha_discovery" in data

    def test_connection_details(self, client):
        resp = client.get("/api/plugins/mqtt/status")
        data = resp.json()
        assert data["connection"]["state"] == "connected"
        assert data["connection"]["broker"] == "localhost"

    def test_state_manager_details(self, client):
        resp = client.get("/api/plugins/mqtt/status")
        data = resp.json()
        assert data["state_manager"]["publish_count"] == 42
        assert data["state_manager"]["command_count"] == 5

    def test_503_when_not_initialised(self, empty_client):
        resp = empty_client.get("/api/plugins/mqtt/status")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /connection endpoint tests
# ---------------------------------------------------------------------------


class TestMqttConnectionEndpoint:
    """Tests for GET /api/plugins/mqtt/connection."""

    def test_returns_connection_status(self, client):
        resp = client.get("/api/plugins/mqtt/connection")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "connected"
        assert data["broker"] == "localhost"
        assert data["port"] == 1883

    def test_503_when_not_initialised(self, empty_client):
        resp = empty_client.get("/api/plugins/mqtt/connection")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /devices endpoint tests
# ---------------------------------------------------------------------------


class TestMqttDevicesEndpoint:
    """Tests for GET /api/plugins/mqtt/devices."""

    def test_returns_device_state_status(self, client):
        resp = client.get("/api/plugins/mqtt/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert data["subscribed"] is True
        assert data["publish_count"] == 42
        assert data["command_count"] == 5
        assert data["error_count"] == 1
        assert data["topic_prefix"] == "casectl"

    def test_503_when_not_initialised(self, empty_client):
        resp = empty_client.get("/api/plugins/mqtt/devices")
        assert resp.status_code == 503
