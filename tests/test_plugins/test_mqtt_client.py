"""Tests for the MQTT client connection manager.

All tests mock aiomqtt so no real broker is needed.
"""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.plugins.mqtt.client import (
    BrokerSettings,
    ConnectionState,
    MqttConnectionManager,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def default_settings() -> BrokerSettings:
    """Return BrokerSettings with default values."""
    return BrokerSettings()


@pytest.fixture()
def custom_settings() -> BrokerSettings:
    """Return BrokerSettings with custom values."""
    return BrokerSettings(
        host="mqtt.example.com",
        port=8883,
        username="testuser",
        password="testpass",
        client_id="casectl-test",
        topic_prefix="test/casectl",
        ha_discovery_prefix="homeassistant",
        qos=1,
        retain=True,
        keepalive=30,
        reconnect_min_delay=0.1,
        reconnect_max_delay=1.0,
        tls_enabled=True,
        tls_ca_cert="",
        tls_insecure=False,
    )


@pytest.fixture()
def mock_aiomqtt():
    """Mock the aiomqtt module and return (module_mock, client_instance_mock)."""
    mock_module = MagicMock()

    # Create a mock client instance
    client_instance = AsyncMock()
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=False)
    client_instance.publish = AsyncMock()
    client_instance.subscribe = AsyncMock()
    client_instance.unsubscribe = AsyncMock()

    # Make messages an async iterator that blocks forever (simulating idle)
    async def _empty_messages():
        # Yield nothing — just block until cancelled
        await asyncio.Event().wait()
        yield  # pragma: no cover — unreachable, makes this an async generator

    client_instance.messages = _empty_messages()

    # Will class mock
    mock_module.Will = MagicMock()
    mock_module.Client = MagicMock(return_value=client_instance)

    return mock_module, client_instance


@pytest.fixture()
def manager(default_settings: BrokerSettings) -> MqttConnectionManager:
    """Return a fresh MqttConnectionManager with default settings."""
    return MqttConnectionManager(default_settings)


# ---------------------------------------------------------------------------
# BrokerSettings tests
# ---------------------------------------------------------------------------


class TestBrokerSettings:
    """Tests for BrokerSettings dataclass."""

    def test_default_values(self) -> None:
        s = BrokerSettings()
        assert s.host == "localhost"
        assert s.port == 1883
        assert s.username == ""
        assert s.password == ""
        assert s.client_id == "casectl"
        assert s.topic_prefix == "casectl"
        assert s.qos == 1
        assert s.retain is True
        assert s.keepalive == 60
        assert s.reconnect_min_delay == 1.0
        assert s.reconnect_max_delay == 60.0
        assert s.tls_enabled is False

    def test_status_topic_default(self) -> None:
        s = BrokerSettings(topic_prefix="myprefix")
        assert s.status_topic == "myprefix/status"

    def test_status_topic_custom_birth(self) -> None:
        s = BrokerSettings(birth_topic="custom/birth")
        assert s.status_topic == "custom/birth"

    def test_will_topic_resolved_default(self) -> None:
        s = BrokerSettings(topic_prefix="casectl")
        assert s.will_topic_resolved == "casectl/status"

    def test_will_topic_resolved_custom(self) -> None:
        s = BrokerSettings(will_topic="custom/will")
        assert s.will_topic_resolved == "custom/will"

    def test_from_config(self) -> None:
        from casectl.config.models import MqttConfig

        cfg = MqttConfig(
            broker_host="mqtt.local",
            broker_port=8883,
            username="user",
            password="pass",
            client_id="test-id",
            topic_prefix="tp",
            qos=2,
            retain=False,
            keepalive=30,
        )
        s = BrokerSettings.from_config(cfg)
        assert s.host == "mqtt.local"
        assert s.port == 8883
        assert s.username == "user"
        assert s.password == "pass"
        assert s.client_id == "test-id"
        assert s.topic_prefix == "tp"
        assert s.qos == 2
        assert s.retain is False
        assert s.keepalive == 30

    def test_frozen(self) -> None:
        s = BrokerSettings()
        with pytest.raises(AttributeError):
            s.host = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ConnectionState tests
# ---------------------------------------------------------------------------


class TestConnectionState:

    def test_values(self) -> None:
        assert ConnectionState.DISCONNECTED.value == "disconnected"
        assert ConnectionState.CONNECTING.value == "connecting"
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.RECONNECTING.value == "reconnecting"
        assert ConnectionState.DISCONNECTING.value == "disconnecting"


# ---------------------------------------------------------------------------
# MqttConnectionManager — initial state
# ---------------------------------------------------------------------------


class TestManagerInitialState:

    def test_default_state(self, manager: MqttConnectionManager) -> None:
        assert manager.state == ConnectionState.DISCONNECTED
        assert manager.is_connected is False
        assert manager.reconnect_count == 0

    def test_settings_accessible(self, default_settings: BrokerSettings) -> None:
        mgr = MqttConnectionManager(default_settings)
        assert mgr.settings is default_settings

    def test_get_status(self, manager: MqttConnectionManager) -> None:
        status = manager.get_status()
        assert status["state"] == "disconnected"
        assert status["broker"] == "localhost"
        assert status["port"] == 1883
        assert status["client_id"] == "casectl"
        assert status["reconnect_count"] == 0
        assert status["subscriptions"] == []


# ---------------------------------------------------------------------------
# MqttConnectionManager — connect / disconnect
# ---------------------------------------------------------------------------


class TestManagerConnect:

    async def test_connect_success(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()

        assert manager.state == ConnectionState.CONNECTED
        assert manager.is_connected is True
        assert manager.reconnect_count == 0

        # Birth message should have been published
        client_instance.publish.assert_called()
        # Check that the birth message was to the status topic
        calls = client_instance.publish.call_args_list
        birth_call = calls[0]
        assert birth_call[0][0] == "casectl/status"
        assert birth_call[1]["payload"] == b"online"

        # Clean up
        await manager.disconnect()

    async def test_connect_already_connected(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, _ = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            # Second connect should be a no-op
            await manager.connect()
            assert manager.is_connected is True

        await manager.disconnect()

    async def test_connect_failure_raises(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt
        client_instance.__aenter__.side_effect = OSError("Connection refused")

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await manager.connect()

        assert manager.state == ConnectionState.DISCONNECTED

    async def test_connect_import_error(self, manager: MqttConnectionManager) -> None:
        """Ensure ImportError is raised if aiomqtt is not installed."""
        with patch.dict("sys.modules", {"aiomqtt": None}):
            with pytest.raises((ImportError, ConnectionError)):
                await manager.connect()

    async def test_disconnect_when_already_disconnected(
        self, manager: MqttConnectionManager
    ) -> None:
        """Disconnect on a disconnected manager should be a no-op."""
        await manager.disconnect()
        assert manager.state == ConnectionState.DISCONNECTED

    async def test_disconnect_publishes_offline(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            client_instance.publish.reset_mock()
            await manager.disconnect()

        assert manager.state == ConnectionState.DISCONNECTED
        # Should have published offline status
        client_instance.publish.assert_called_once_with(
            "casectl/status",
            payload=b"offline",
            qos=1,
            retain=True,
        )

    async def test_context_manager(
        self, default_settings: BrokerSettings, mock_aiomqtt: tuple
    ) -> None:
        mock_module, _ = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            async with MqttConnectionManager(default_settings) as mgr:
                assert mgr.is_connected is True
            assert mgr.state == ConnectionState.DISCONNECTED


# ---------------------------------------------------------------------------
# MqttConnectionManager — publish
# ---------------------------------------------------------------------------


class TestManagerPublish:

    async def test_publish_string(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            client_instance.publish.reset_mock()

            await manager.publish("casectl/test", "hello")

        client_instance.publish.assert_called_once_with(
            "casectl/test",
            payload=b"hello",
            qos=1,
            retain=True,
        )
        await manager.disconnect()

    async def test_publish_bytes(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            client_instance.publish.reset_mock()

            await manager.publish("casectl/test", b"\x01\x02")

        client_instance.publish.assert_called_once_with(
            "casectl/test",
            payload=b"\x01\x02",
            qos=1,
            retain=True,
        )
        await manager.disconnect()

    async def test_publish_custom_qos_retain(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            client_instance.publish.reset_mock()

            await manager.publish("casectl/test", "data", qos=0, retain=False)

        client_instance.publish.assert_called_once_with(
            "casectl/test",
            payload=b"data",
            qos=0,
            retain=False,
        )
        await manager.disconnect()

    async def test_publish_not_connected_raises(
        self, manager: MqttConnectionManager
    ) -> None:
        with pytest.raises(RuntimeError, match="not connected"):
            await manager.publish("topic", "data")


# ---------------------------------------------------------------------------
# MqttConnectionManager — subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestManagerSubscribe:

    async def test_subscribe_while_connected(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt
        callback = AsyncMock()

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            await manager.subscribe("casectl/fan/set", callback)

        client_instance.subscribe.assert_called_with("casectl/fan/set", qos=1)
        assert len(manager.get_status()["subscriptions"]) == 1
        await manager.disconnect()

    async def test_subscribe_while_disconnected(
        self, manager: MqttConnectionManager
    ) -> None:
        """Subscriptions registered while disconnected are stored for later."""
        callback = AsyncMock()
        await manager.subscribe("casectl/fan/set", callback)
        assert "casectl/fan/set" in manager.get_status()["subscriptions"]

    async def test_subscribe_custom_qos(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt
        callback = AsyncMock()

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            await manager.subscribe("casectl/test", callback, qos=2)

        client_instance.subscribe.assert_called_with("casectl/test", qos=2)
        await manager.disconnect()

    async def test_unsubscribe(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, client_instance = mock_aiomqtt
        callback = AsyncMock()

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            await manager.subscribe("casectl/fan/set", callback)
            await manager.unsubscribe("casectl/fan/set")

        client_instance.unsubscribe.assert_called_with("casectl/fan/set")
        assert manager.get_status()["subscriptions"] == []
        await manager.disconnect()

    async def test_subscriptions_restored_on_reconnect(
        self, mock_aiomqtt: tuple
    ) -> None:
        """Pre-registered subscriptions should be restored after connect."""
        mock_module, client_instance = mock_aiomqtt
        settings = BrokerSettings(reconnect_min_delay=0.1, reconnect_max_delay=0.2)
        mgr = MqttConnectionManager(settings)

        callback = AsyncMock()
        # Register subscription while disconnected
        await mgr.subscribe("casectl/fan/set", callback, qos=1)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await mgr.connect()

        # The subscription should have been applied
        client_instance.subscribe.assert_called_with("casectl/fan/set", qos=1)
        await mgr.disconnect()


# ---------------------------------------------------------------------------
# MqttConnectionManager — message dispatch
# ---------------------------------------------------------------------------


class TestManagerDispatch:

    async def test_dispatch_exact_match(
        self, manager: MqttConnectionManager
    ) -> None:
        callback = AsyncMock()
        await manager.subscribe("casectl/fan/set", callback)

        await manager._dispatch_message("casectl/fan/set", b"128")

        callback.assert_called_once_with("casectl/fan/set", b"128")

    async def test_dispatch_wildcard_plus(
        self, manager: MqttConnectionManager
    ) -> None:
        callback = AsyncMock()
        await manager.subscribe("casectl/+/set", callback)

        await manager._dispatch_message("casectl/fan/set", b"128")

        callback.assert_called_once_with("casectl/fan/set", b"128")

    async def test_dispatch_no_match(
        self, manager: MqttConnectionManager
    ) -> None:
        callback = AsyncMock()
        await manager.subscribe("casectl/fan/set", callback)

        await manager._dispatch_message("casectl/led/set", b"on")

        callback.assert_not_called()

    async def test_dispatch_callback_error_logged(
        self, manager: MqttConnectionManager
    ) -> None:
        """A failing callback should not prevent other dispatch."""
        bad_cb = AsyncMock(side_effect=ValueError("boom"))
        good_cb = AsyncMock()

        await manager.subscribe("casectl/test", bad_cb)
        await manager.subscribe("casectl/test", good_cb)

        await manager._dispatch_message("casectl/test", b"data")

        bad_cb.assert_called_once()
        good_cb.assert_called_once()

    async def test_dispatch_sync_callback(
        self, manager: MqttConnectionManager
    ) -> None:
        """Sync callbacks should work too."""
        results: list[tuple[str, bytes]] = []

        def sync_cb(topic: str, payload: bytes) -> None:
            results.append((topic, payload))

        await manager.subscribe("casectl/test", sync_cb)
        await manager._dispatch_message("casectl/test", b"hello")

        assert results == [("casectl/test", b"hello")]


# ---------------------------------------------------------------------------
# MqttConnectionManager — state listeners
# ---------------------------------------------------------------------------


class TestManagerStateListeners:

    async def test_state_change_listener(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, _ = mock_aiomqtt
        states: list[ConnectionState] = []
        manager.on_state_change(lambda s: states.append(s))

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            await manager.disconnect()

        assert ConnectionState.CONNECTING in states
        assert ConnectionState.CONNECTED in states
        assert ConnectionState.DISCONNECTING in states
        assert ConnectionState.DISCONNECTED in states

    async def test_state_change_listener_error_ignored(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        """A failing listener should not prevent state transitions."""
        mock_module, _ = mock_aiomqtt

        def bad_listener(state: ConnectionState) -> None:
            raise RuntimeError("listener error")

        manager.on_state_change(bad_listener)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            # Should not raise despite the bad listener
            await manager.connect()
            assert manager.is_connected is True
            await manager.disconnect()


# ---------------------------------------------------------------------------
# MqttConnectionManager — TLS
# ---------------------------------------------------------------------------


class TestManagerTLS:

    def test_tls_disabled_returns_none(self) -> None:
        settings = BrokerSettings(tls_enabled=False)
        mgr = MqttConnectionManager(settings)
        assert mgr._build_tls_context() is None

    def test_tls_enabled_returns_context(self) -> None:
        settings = BrokerSettings(tls_enabled=True)
        mgr = MqttConnectionManager(settings)
        ctx = mgr._build_tls_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_tls_insecure_skips_verification(self) -> None:
        settings = BrokerSettings(tls_enabled=True, tls_insecure=True)
        mgr = MqttConnectionManager(settings)
        ctx = mgr._build_tls_context()
        assert ctx is not None
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_tls_ca_cert_missing_file(self, tmp_path) -> None:
        settings = BrokerSettings(
            tls_enabled=True,
            tls_ca_cert=str(tmp_path / "nonexistent.pem"),
        )
        mgr = MqttConnectionManager(settings)
        # Should not raise — just warns and uses system CAs
        ctx = mgr._build_tls_context()
        assert isinstance(ctx, ssl.SSLContext)


# ---------------------------------------------------------------------------
# MqttConnectionManager — reconnection
# ---------------------------------------------------------------------------


class TestManagerReconnect:

    async def test_reconnect_loop_increments_count(self) -> None:
        """Verify reconnect attempts increment the counter."""
        settings = BrokerSettings(reconnect_min_delay=0.01, reconnect_max_delay=0.02)
        mgr = MqttConnectionManager(settings)

        attempt_count = 0
        original_do_connect = mgr._do_connect

        async def failing_connect():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise OSError("Connection refused")
            # Succeed on third attempt
            await original_do_connect()

        mock_module = MagicMock()
        client_instance = AsyncMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        client_instance.publish = AsyncMock()
        client_instance.subscribe = AsyncMock()

        async def _empty_msgs():
            await asyncio.Event().wait()
            yield  # pragma: no cover

        client_instance.messages = _empty_msgs()
        mock_module.Will = MagicMock()
        mock_module.Client = MagicMock(return_value=client_instance)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            mgr._do_connect = failing_connect
            mgr._set_state(ConnectionState.RECONNECTING)
            await mgr._reconnect_loop()

        # Should have attempted 3 times (2 failures + 1 success)
        assert attempt_count == 3
        await mgr.disconnect()

    async def test_reconnect_stops_on_disconnect(self) -> None:
        """Setting stop_event should exit the reconnect loop."""
        settings = BrokerSettings(reconnect_min_delay=0.01, reconnect_max_delay=0.02)
        mgr = MqttConnectionManager(settings)

        async def always_fail():
            raise OSError("nope")

        mgr._do_connect = always_fail

        # Set stop event after a short delay
        async def stop_soon():
            await asyncio.sleep(0.05)
            mgr._stop_event.set()

        task = asyncio.create_task(stop_soon())
        await mgr._reconnect_loop()
        await task

        assert mgr.state == ConnectionState.DISCONNECTED


# ---------------------------------------------------------------------------
# MqttConnectionManager — wait_connected
# ---------------------------------------------------------------------------


class TestManagerWaitConnected:

    async def test_wait_connected_success(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        mock_module, _ = mock_aiomqtt

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()
            result = await manager.wait_connected(timeout=1.0)

        assert result is True
        await manager.disconnect()

    async def test_wait_connected_timeout(
        self, manager: MqttConnectionManager
    ) -> None:
        result = await manager.wait_connected(timeout=0.05)
        assert result is False


# ---------------------------------------------------------------------------
# MqttConfig model tests
# ---------------------------------------------------------------------------


class TestMqttConfig:
    """Verify the Pydantic MqttConfig model in config/models.py."""

    def test_default_values(self) -> None:
        from casectl.config.models import MqttConfig

        cfg = MqttConfig()
        assert cfg.enabled is False
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883
        assert cfg.qos == 1
        assert cfg.retain is True
        assert cfg.keepalive == 60

    def test_qos_validation(self) -> None:
        from casectl.config.models import MqttConfig

        with pytest.raises(ValueError):
            MqttConfig(qos=3)

    def test_port_validation(self) -> None:
        from casectl.config.models import MqttConfig

        with pytest.raises(ValueError):
            MqttConfig(broker_port=0)

    def test_in_root_config(self) -> None:
        from casectl.config.models import CaseCtlConfig

        cfg = CaseCtlConfig()
        assert hasattr(cfg, "mqtt")
        assert cfg.mqtt.enabled is False
        assert cfg.mqtt.broker_host == "localhost"

    def test_custom_values(self) -> None:
        from casectl.config.models import MqttConfig

        cfg = MqttConfig(
            enabled=True,
            broker_host="mqtt.local",
            broker_port=8883,
            username="user",
            password="pass",
            client_id="myctl",
            topic_prefix="home/casectl",
            qos=2,
            retain=False,
            keepalive=30,
            tls_enabled=True,
        )
        assert cfg.enabled is True
        assert cfg.broker_host == "mqtt.local"
        assert cfg.broker_port == 8883
        assert cfg.qos == 2
        assert cfg.tls_enabled is True


# ---------------------------------------------------------------------------
# Will message configuration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MqttConnectionManager — edge cases & error paths
# ---------------------------------------------------------------------------


class TestManagerEdgeCases:

    async def test_disconnect_cancels_reconnect_task(
        self, mock_aiomqtt: tuple
    ) -> None:
        """Disconnect should cancel an in-progress reconnect task."""
        mock_module, client_instance = mock_aiomqtt
        settings = BrokerSettings(reconnect_min_delay=0.01, reconnect_max_delay=0.02)
        mgr = MqttConnectionManager(settings)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await mgr.connect()

        # Simulate a reconnect task being active
        async def fake_reconnect():
            await asyncio.sleep(100)

        mgr._reconnect_task = asyncio.create_task(fake_reconnect())
        await mgr.disconnect()
        assert mgr.state == ConnectionState.DISCONNECTED

    async def test_disconnect_handles_publish_error(
        self, mock_aiomqtt: tuple
    ) -> None:
        """Disconnect should still complete even if offline publish fails."""
        mock_module, client_instance = mock_aiomqtt
        settings = BrokerSettings()
        mgr = MqttConnectionManager(settings)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await mgr.connect()

        # Make publish fail
        client_instance.publish.side_effect = OSError("broken pipe")
        await mgr.disconnect()
        assert mgr.state == ConnectionState.DISCONNECTED

    async def test_disconnect_handles_aexit_error(
        self, mock_aiomqtt: tuple
    ) -> None:
        """Disconnect should still complete even if __aexit__ fails."""
        mock_module, client_instance = mock_aiomqtt
        settings = BrokerSettings()
        mgr = MqttConnectionManager(settings)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await mgr.connect()

        client_instance.__aexit__.side_effect = OSError("close error")
        await mgr.disconnect()
        assert mgr.state == ConnectionState.DISCONNECTED

    async def test_message_loop_with_none_client(
        self, manager: MqttConnectionManager
    ) -> None:
        """Message loop should return immediately if client is None."""
        manager._client = None
        await manager._message_loop()  # Should not raise

    async def test_message_loop_error_triggers_reconnect(self) -> None:
        """An error in the message loop should schedule a reconnect."""
        settings = BrokerSettings(reconnect_min_delay=0.01, reconnect_max_delay=0.02)
        mgr = MqttConnectionManager(settings)

        # Create a mock client whose messages raises
        mock_client = MagicMock()

        async def _error_messages():
            raise OSError("connection lost")
            yield  # pragma: no cover

        mock_client.messages = _error_messages()
        mgr._client = mock_client
        mgr._set_state(ConnectionState.CONNECTED)

        # Patch _schedule_reconnect to track it was called
        reconnect_called = False
        original_schedule = mgr._schedule_reconnect

        async def track_reconnect():
            nonlocal reconnect_called
            reconnect_called = True
            # Don't actually reconnect in the test
            mgr._stop_event.set()

        mgr._schedule_reconnect = track_reconnect
        await mgr._message_loop()
        assert reconnect_called

    async def test_schedule_reconnect_skips_if_stopped(
        self, manager: MqttConnectionManager
    ) -> None:
        """_schedule_reconnect should be a no-op if stop_event is set."""
        manager._stop_event.set()
        await manager._schedule_reconnect()
        assert manager._reconnect_task is None

    async def test_schedule_reconnect_skips_if_already_running(
        self, manager: MqttConnectionManager
    ) -> None:
        """_schedule_reconnect should not create a second task."""
        # Create a fake running task
        async def fake():
            await asyncio.sleep(100)

        manager._reconnect_task = asyncio.create_task(fake())
        await manager._schedule_reconnect()  # Should not create another
        manager._reconnect_task.cancel()
        try:
            await manager._reconnect_task
        except asyncio.CancelledError:
            pass

    async def test_birth_message_failure_logged(
        self, mock_aiomqtt: tuple
    ) -> None:
        """Birth publish failure should be logged, not raised."""
        mock_module, client_instance = mock_aiomqtt
        settings = BrokerSettings()
        mgr = MqttConnectionManager(settings)

        # Make publish fail (birth will fail during connect)
        client_instance.publish.side_effect = OSError("publish error")

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            # Should not raise despite birth message failure
            await mgr.connect()
            assert mgr.is_connected is True

        await mgr.disconnect()

    async def test_restore_subscriptions_failure_logged(
        self, mock_aiomqtt: tuple
    ) -> None:
        """Restore subscription failure should be logged, not raised."""
        mock_module, client_instance = mock_aiomqtt
        settings = BrokerSettings()
        mgr = MqttConnectionManager(settings)

        callback = AsyncMock()
        await mgr.subscribe("failing/topic", callback)

        # Make subscribe fail
        client_instance.subscribe.side_effect = OSError("subscribe error")

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            # _restore_subscriptions should not raise
            await mgr.connect()
            assert mgr.is_connected is True

        await mgr.disconnect()

    async def test_set_state_no_op_for_same_state(
        self, manager: MqttConnectionManager
    ) -> None:
        """Setting the same state should not notify listeners."""
        calls: list[ConnectionState] = []
        manager.on_state_change(lambda s: calls.append(s))

        manager._set_state(ConnectionState.DISCONNECTED)  # Same as initial
        assert calls == []

    async def test_async_state_listener(
        self, manager: MqttConnectionManager, mock_aiomqtt: tuple
    ) -> None:
        """Async state listeners should work."""
        mock_module, _ = mock_aiomqtt
        states: list[ConnectionState] = []

        async def async_listener(state: ConnectionState) -> None:
            states.append(state)

        manager.on_state_change(async_listener)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await manager.connect()

        # Allow scheduled coroutines to run
        await asyncio.sleep(0)

        assert ConnectionState.CONNECTING in states
        assert ConnectionState.CONNECTED in states
        await manager.disconnect()


class TestWillMessage:

    async def test_will_message_configured(
        self, default_settings: BrokerSettings, mock_aiomqtt: tuple
    ) -> None:
        """Verify that the MQTT client is created with a Will message."""
        mock_module, _ = mock_aiomqtt
        mgr = MqttConnectionManager(default_settings)

        with patch.dict("sys.modules", {"aiomqtt": mock_module}):
            await mgr.connect()

        # Check that Will was constructed
        mock_module.Will.assert_called_once_with(
            topic="casectl/status",
            payload=b"offline",
            qos=1,
            retain=True,
        )

        # Check that Client was constructed with the will
        client_call = mock_module.Client.call_args
        assert client_call[1]["will"] is not None
        await mgr.disconnect()
