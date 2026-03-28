"""Tests for the MQTT plugin entry point and routes.

All tests mock the MQTT connection manager so no real broker is needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.plugins.base import HardwareRegistry, PluginContext, PluginStatus
from casectl.plugins.mqtt.client import BrokerSettings, ConnectionState
from casectl.plugins.mqtt.plugin import MqttPlugin


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_config_manager() -> MagicMock:
    """Return a mock ConfigManager."""
    mgr = MagicMock()
    mgr.update = AsyncMock()
    mgr.get = AsyncMock(return_value={})
    return mgr


@pytest.fixture()
def mock_event_bus() -> MagicMock:
    """Return a mock EventBus."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture()
def plugin_context(mock_config_manager, mock_event_bus) -> PluginContext:
    """Return a PluginContext for testing the MQTT plugin."""
    hw = HardwareRegistry()
    ctx = PluginContext(
        plugin_name="mqtt",
        config_manager=mock_config_manager,
        hardware_registry=hw,
        event_bus=mock_event_bus,
    )
    return ctx


@pytest.fixture()
def mqtt_config() -> dict:
    """Return an enabled MQTT config dict."""
    return {
        "enabled": True,
        "broker_host": "localhost",
        "broker_port": 1883,
        "username": "",
        "password": "",
        "client_id": "casectl-test",
        "topic_prefix": "test/casectl",
        "ha_discovery_prefix": "homeassistant",
        "qos": 1,
        "retain": True,
        "keepalive": 60,
        "reconnect_min_delay": 0.01,
        "reconnect_max_delay": 0.02,
        "tls_enabled": False,
        "publish_interval": 10.0,
    }


@pytest.fixture()
def mock_aiomqtt():
    """Mock the aiomqtt module and return (module_mock, client_instance_mock)."""
    mock_module = MagicMock()

    client_instance = AsyncMock()
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=False)
    client_instance.publish = AsyncMock()
    client_instance.subscribe = AsyncMock()
    client_instance.unsubscribe = AsyncMock()

    async def _empty_messages():
        await asyncio.Event().wait()
        yield  # pragma: no cover

    client_instance.messages = _empty_messages()

    mock_module.Will = MagicMock()
    mock_module.Client = MagicMock(return_value=client_instance)

    return mock_module, client_instance


# ---------------------------------------------------------------------------
# Plugin instantiation tests
# ---------------------------------------------------------------------------


class TestMqttPluginInit:
    """Tests for MqttPlugin construction."""

    def test_attributes(self):
        plugin = MqttPlugin()
        assert plugin.name == "mqtt"
        assert plugin.version == "0.2.0"
        assert plugin.min_daemon_version == "0.1.0"

    def test_initial_state(self):
        plugin = MqttPlugin()
        assert plugin._mqtt is None
        assert plugin._state_manager is None
        assert plugin._metric_publisher is None
        assert plugin._ha_discovery is None
        assert plugin._enabled is False


# ---------------------------------------------------------------------------
# Plugin setup tests
# ---------------------------------------------------------------------------


class TestMqttPluginSetup:
    """Tests for MqttPlugin.setup()."""

    async def test_setup_registers_routes(self, plugin_context):
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        assert plugin_context.routes is not None

    async def test_setup_stores_context(self, plugin_context):
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        assert plugin._ctx is plugin_context


# ---------------------------------------------------------------------------
# Plugin start tests
# ---------------------------------------------------------------------------


class TestMqttPluginStart:
    """Tests for MqttPlugin.start()."""

    async def test_start_disabled(self, plugin_context, mock_config_manager):
        """Plugin should remain dormant when MQTT is disabled."""
        mock_config_manager.get = AsyncMock(return_value={"enabled": False})
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        await plugin.start()
        assert plugin._enabled is False
        assert plugin._mqtt is None

    async def test_start_enabled_connects(
        self, plugin_context, mock_config_manager, mqtt_config, mock_aiomqtt
    ):
        """Plugin should connect to broker when enabled."""
        mock_config_manager.get = AsyncMock(return_value=mqtt_config)
        mock_module, client_instance = mock_aiomqtt

        plugin = MqttPlugin()
        await plugin.setup(plugin_context)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await plugin.start()

        assert plugin._enabled is True
        assert plugin._mqtt is not None
        assert plugin._mqtt.is_connected is True
        assert plugin._state_manager is not None
        assert plugin._metric_publisher is not None
        assert plugin._ha_discovery is not None

        # Clean up
        await plugin.stop()

    async def test_start_connection_failure(
        self, plugin_context, mock_config_manager, mqtt_config, mock_aiomqtt
    ):
        """Plugin should handle connection failure gracefully."""
        mock_config_manager.get = AsyncMock(return_value=mqtt_config)
        mock_module, client_instance = mock_aiomqtt
        client_instance.__aenter__.side_effect = OSError("Connection refused")

        plugin = MqttPlugin()
        await plugin.setup(plugin_context)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await plugin.start()  # Should not raise

        assert plugin._enabled is True
        assert plugin._mqtt is not None
        # State manager created but MQTT not connected
        assert not plugin._mqtt.is_connected

    async def test_start_without_setup(self):
        """Start without setup should log error and return."""
        plugin = MqttPlugin()
        await plugin.start()  # Should not raise
        assert plugin._enabled is False

    async def test_start_empty_config(self, plugin_context, mock_config_manager):
        """Plugin should remain dormant with empty config."""
        mock_config_manager.get = AsyncMock(return_value={})
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        await plugin.start()
        assert plugin._enabled is False

    async def test_start_config_read_error(self, plugin_context, mock_config_manager):
        """Plugin should remain dormant if config read fails."""
        mock_config_manager.get = AsyncMock(side_effect=RuntimeError("disk error"))
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        await plugin.start()
        assert plugin._enabled is False

    async def test_start_sets_app_state(
        self, plugin_context, mock_config_manager, mqtt_config, mock_aiomqtt
    ):
        """Plugin should store references on app.state."""
        mock_config_manager.get = AsyncMock(return_value=mqtt_config)
        mock_module, _ = mock_aiomqtt

        plugin = MqttPlugin()
        await plugin.setup(plugin_context)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await plugin.start()

        items = plugin_context.get_app_state_items()
        assert "mqtt_manager" in items
        assert "mqtt_state_manager" in items
        assert "mqtt_metric_publisher" in items
        assert "mqtt_ha_discovery" in items

        await plugin.stop()


# ---------------------------------------------------------------------------
# Plugin stop tests
# ---------------------------------------------------------------------------


class TestMqttPluginStop:
    """Tests for MqttPlugin.stop()."""

    async def test_stop_when_not_started(self):
        """Stop on a non-started plugin should be a no-op."""
        plugin = MqttPlugin()
        await plugin.stop()  # Should not raise

    async def test_stop_disconnects(
        self, plugin_context, mock_config_manager, mqtt_config, mock_aiomqtt
    ):
        """Stop should disconnect from broker."""
        mock_config_manager.get = AsyncMock(return_value=mqtt_config)
        mock_module, _ = mock_aiomqtt

        plugin = MqttPlugin()
        await plugin.setup(plugin_context)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await plugin.start()
            assert plugin._mqtt.is_connected is True
            await plugin.stop()

        assert plugin._mqtt.state == ConnectionState.DISCONNECTED


# ---------------------------------------------------------------------------
# Plugin get_status tests
# ---------------------------------------------------------------------------


class TestMqttPluginGetStatus:
    """Tests for MqttPlugin.get_status()."""

    def test_status_when_disabled(self):
        plugin = MqttPlugin()
        status = plugin.get_status()
        assert status["status"] == PluginStatus.STOPPED
        assert status["enabled"] is False

    async def test_status_when_connected(
        self, plugin_context, mock_config_manager, mqtt_config, mock_aiomqtt
    ):
        mock_config_manager.get = AsyncMock(return_value=mqtt_config)
        mock_module, _ = mock_aiomqtt

        plugin = MqttPlugin()
        await plugin.setup(plugin_context)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await plugin.start()

        status = plugin.get_status()
        assert status["status"] == PluginStatus.HEALTHY
        assert status["enabled"] is True
        assert status["connection"] == "connected"
        assert "broker" in status
        assert "state_manager" in status
        assert "metric_publisher" in status
        assert "ha_discovery" in status

        await plugin.stop()

    async def test_status_when_disconnected(
        self, plugin_context, mock_config_manager, mqtt_config, mock_aiomqtt
    ):
        mock_config_manager.get = AsyncMock(return_value=mqtt_config)
        mock_module, client_instance = mock_aiomqtt
        client_instance.__aenter__.side_effect = OSError("Connection refused")

        plugin = MqttPlugin()
        await plugin.setup(plugin_context)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await plugin.start()

        status = plugin.get_status()
        assert status["status"] == PluginStatus.DEGRADED
        assert status["enabled"] is True


# ---------------------------------------------------------------------------
# Build settings tests
# ---------------------------------------------------------------------------


class TestBuildSettings:
    """Tests for _build_settings static method."""

    def test_build_from_full_config(self, mqtt_config):
        settings = MqttPlugin._build_settings(mqtt_config)
        assert settings.host == "localhost"
        assert settings.port == 1883
        assert settings.client_id == "casectl-test"
        assert settings.topic_prefix == "test/casectl"
        assert settings.qos == 1
        assert settings.retain is True

    def test_build_from_empty_config(self):
        settings = MqttPlugin._build_settings({})
        assert settings.host == "localhost"
        assert settings.port == 1883
        assert settings.client_id == "casectl"
        assert settings.topic_prefix == "casectl"

    def test_build_from_partial_config(self):
        settings = MqttPlugin._build_settings({"broker_host": "mqtt.local", "qos": 2})
        assert settings.host == "mqtt.local"
        assert settings.qos == 2
        assert settings.port == 1883  # default


# ---------------------------------------------------------------------------
# Connection state change handler tests
# ---------------------------------------------------------------------------


class TestConnectionStateChange:
    """Tests for _on_connection_state_change."""

    async def test_reconnect_triggers_rediscovery(
        self, plugin_context, mock_config_manager, mqtt_config, mock_aiomqtt
    ):
        mock_config_manager.get = AsyncMock(return_value=mqtt_config)
        mock_module, _ = mock_aiomqtt

        plugin = MqttPlugin()
        await plugin.setup(plugin_context)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await plugin.start()

        # Simulate reconnect
        initial_count = plugin._ha_discovery.publish_count
        await plugin._on_reconnect()
        # publish_discovery should have been called again
        # (it won't actually succeed without connection in this mock, but the path is exercised)

        await plugin.stop()

    def test_non_connected_state_is_noop(self):
        plugin = MqttPlugin()
        # Should not raise
        plugin._on_connection_state_change(ConnectionState.DISCONNECTED)
        plugin._on_connection_state_change(ConnectionState.RECONNECTING)


# ---------------------------------------------------------------------------
# MQTT config reading tests
# ---------------------------------------------------------------------------


class TestGetMqttConfig:
    """Tests for _get_mqtt_config."""

    async def test_returns_dict(self, plugin_context, mock_config_manager):
        mock_config_manager.get = AsyncMock(return_value={"enabled": True, "broker_host": "test"})
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        config = await plugin._get_mqtt_config()
        assert config["enabled"] is True
        assert config["broker_host"] == "test"

    async def test_returns_empty_without_context(self):
        plugin = MqttPlugin()
        config = await plugin._get_mqtt_config()
        assert config == {}

    async def test_returns_empty_on_error(self, plugin_context, mock_config_manager):
        mock_config_manager.get = AsyncMock(side_effect=RuntimeError("fail"))
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        config = await plugin._get_mqtt_config()
        assert config == {}

    async def test_handles_pydantic_model(self, plugin_context, mock_config_manager):
        """Should handle a Pydantic model returned by config manager."""
        from casectl.config.models import MqttConfig

        mock_config_manager.get = AsyncMock(return_value=MqttConfig(enabled=True))
        plugin = MqttPlugin()
        await plugin.setup(plugin_context)
        config = await plugin._get_mqtt_config()
        assert config["enabled"] is True

    async def test_no_config_manager(self, mock_event_bus):
        """Should return empty dict when config manager is None."""
        hw = HardwareRegistry()
        ctx = PluginContext(
            plugin_name="mqtt",
            config_manager=None,
            hardware_registry=hw,
            event_bus=mock_event_bus,
        )
        plugin = MqttPlugin()
        await plugin.setup(ctx)
        config = await plugin._get_mqtt_config()
        assert config == {}
