"""Tests for casectl.daemon.server (FastAPI application factory).

Uses FastAPI TestClient with mocked PluginHost, ConfigManager, and EventBus
to exercise health endpoints, auth middleware, WebSocket, and config routes.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from casectl.daemon.event_bus import EventBus
from casectl.plugins.base import PluginStatus


# ---------------------------------------------------------------------------
# Helpers: Build a test app with mocked dependencies
# ---------------------------------------------------------------------------


def _make_test_app(
    *,
    api_token: str | None = None,
    host: str = "127.0.0.1",
) -> tuple[TestClient, MagicMock, MagicMock, EventBus]:
    """Create a FastAPI app via create_app() with mock dependencies.

    Returns (TestClient, mock_plugin_host, mock_config_manager, event_bus).
    """
    plugin_host = MagicMock()
    plugin_host.list_plugins.return_value = [
        {
            "name": "mock-plugin",
            "version": "0.1.0",
            "status": "healthy",
            "description": "Test plugin",
        },
    ]
    plugin_host.get_routes.return_value = []
    plugin_host.get_all_statuses.return_value = {"mock-plugin": PluginStatus.HEALTHY}
    plugin_host.get_plugin.return_value = None
    plugin_host.start_all = AsyncMock()
    plugin_host.stop_all = AsyncMock()

    config_manager = MagicMock()
    config_manager.get = AsyncMock(return_value={"mode": 0, "manual_duty": [75, 75, 75]})
    config_manager.update = AsyncMock()

    event_bus = EventBus(max_ws=10)

    env = {}
    if api_token is not None:
        env["CASECTL_API_TOKEN"] = api_token

    with patch.dict(os.environ, env, clear=False):
        from casectl.daemon.server import create_app

        app = create_app(
            plugin_host=plugin_host,
            config_manager=config_manager,
            event_bus=event_bus,
            host=host,
        )

    client = TestClient(app, raise_server_exceptions=False)
    return client, plugin_host, config_manager, event_bus


# ---------------------------------------------------------------------------
# Tests: Health endpoint
# ---------------------------------------------------------------------------


def test_health_returns_expected_fields() -> None:
    """GET /api/health returns status, uptime, version, and plugins."""
    client, _, _, _ = _make_test_app()
    resp = client.get("/api/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "running"
    assert "uptime" in body
    assert "version" in body
    assert isinstance(body["plugins"], list)
    assert len(body["plugins"]) == 1
    assert body["plugins"][0]["name"] == "mock-plugin"


# ---------------------------------------------------------------------------
# Tests: Token authentication
# ---------------------------------------------------------------------------


def test_auth_no_token_localhost_gets_200() -> None:
    """When bound to localhost and no token set, requests pass through."""
    client, _, _, _ = _make_test_app(api_token=None, host="127.0.0.1")
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_auth_with_token_no_credentials_gets_401() -> None:
    """When a token is configured, unauthenticated remote requests get 401.

    TestClient doesn't set client.host to 127.0.0.1, so the localhost
    bypass does not apply — requests are treated as remote.
    """
    client, _, _, _ = _make_test_app(api_token="secret123", host="0.0.0.0")
    resp = client.get("/api/health")
    assert resp.status_code == 401  # no token provided, not localhost


def test_auth_valid_query_token_gets_200() -> None:
    """Request with valid ?token= query parameter is authorized."""
    client, _, _, _ = _make_test_app(api_token="secret123", host="0.0.0.0")
    resp = client.get("/api/health?token=secret123")
    assert resp.status_code == 200


def test_auth_valid_cookie_gets_200() -> None:
    """Request with valid casectl_token cookie is authorized."""
    client, _, _, _ = _make_test_app(api_token="secret123", host="0.0.0.0")
    client.cookies.set("casectl_token", "secret123")
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_auth_valid_bearer_gets_200() -> None:
    """Request with valid Bearer token header is authorized."""
    client, _, _, _ = _make_test_app(api_token="secret123", host="0.0.0.0")
    resp = client.get(
        "/api/health",
        headers={"Authorization": "Bearer secret123"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: Config endpoints
# ---------------------------------------------------------------------------


def test_get_config_section() -> None:
    """GET /api/config/{section} returns the section data."""
    client, _, config_manager, _ = _make_test_app()
    resp = client.get("/api/config/fan")
    assert resp.status_code == 200
    config_manager.get.assert_called_once_with("fan")


def test_patch_config_updates() -> None:
    """PATCH /api/config updates the specified section."""
    client, _, config_manager, _ = _make_test_app()

    # Mock update to return a config-like object with model_dump
    mock_config = MagicMock()
    mock_config.model_dump.return_value = {"fan": {"mode": 2}}
    config_manager.update = AsyncMock(return_value=mock_config)

    resp = client.patch(
        "/api/config",
        json={"section": "fan", "values": {"mode": 2}},
    )
    assert resp.status_code == 200
    config_manager.update.assert_called_once_with("fan", {"mode": 2})


def test_patch_config_missing_section_returns_422() -> None:
    """PATCH /api/config without 'section' key returns 422 (validation error)."""
    client, _, _, _ = _make_test_app()
    resp = client.patch("/api/config", json={"mode": 2})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: WebSocket
# ---------------------------------------------------------------------------


def test_websocket_connects() -> None:
    """WebSocket at /api/ws can be connected."""
    client, _, _, event_bus = _make_test_app()
    with client.websocket_connect("/api/ws") as ws:
        # Connection established -- just verify it does not raise
        assert event_bus.ws_count >= 0


# ---------------------------------------------------------------------------
# Tests: WebSocket OLED commands
# ---------------------------------------------------------------------------

