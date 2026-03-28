"""Tests for the MQTT metric publisher.

All tests mock the MQTT connection manager so no real broker is needed.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from casectl.plugins.mqtt.client import BrokerSettings, MqttConnectionManager
from casectl.plugins.mqtt.metrics import MetricPublisher, _METRIC_FIELDS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def broker_settings() -> BrokerSettings:
    """Return BrokerSettings with a fast publish interval for tests."""
    return BrokerSettings(
        topic_prefix="casectl",
        qos=1,
        retain=True,
        publish_interval=0.05,  # 50ms for fast tests
    )


@pytest.fixture()
def mock_mqtt(broker_settings: BrokerSettings) -> MqttConnectionManager:
    """Return a mock MqttConnectionManager that appears connected."""
    mgr = MagicMock(spec=MqttConnectionManager)
    mgr.settings = broker_settings
    mgr.is_connected = True
    mgr.publish = AsyncMock()
    return mgr


@pytest.fixture()
def mock_event_bus():
    """Return a mock EventBus."""
    from casectl.daemon.event_bus import EventBus

    return EventBus()


@pytest.fixture()
def sample_metrics() -> dict:
    """Return a realistic metrics snapshot."""
    return {
        "cpu_percent": 42.5,
        "memory_percent": 61.2,
        "disk_percent": 34.7,
        "cpu_temp": 58.3,
        "case_temp": 31.0,
        "ip_address": "192.168.1.42",
        "fan_duty": [128, 128, 128],
        "motor_speed": [1200, 1180, 1190],
        "rpi_fan_duty": 200,
        "date": "2026-03-28",
        "weekday": "Saturday",
        "time": "14:30:00",
    }


@pytest.fixture()
def publisher(mock_mqtt, mock_event_bus) -> MetricPublisher:
    """Return a MetricPublisher with mocked dependencies."""
    return MetricPublisher(mock_mqtt, mock_event_bus)


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestMetricPublisherInit:

    def test_default_properties(self, mock_mqtt) -> None:
        pub = MetricPublisher(mock_mqtt)
        assert pub.topic_prefix == "casectl"
        assert pub.publish_interval == 0.05  # from broker_settings fixture
        assert pub.publish_count == 0
        assert pub.error_count == 0
        assert pub.is_running is False
        assert pub.latest_metrics is None

    def test_custom_prefix_and_interval(self, mock_mqtt) -> None:
        pub = MetricPublisher(
            mock_mqtt,
            topic_prefix="custom/prefix",
            publish_interval=30.0,
        )
        assert pub.topic_prefix == "custom/prefix"
        assert pub.publish_interval == 30.0

    def test_defaults_from_settings(self, broker_settings) -> None:
        mgr = MagicMock(spec=MqttConnectionManager)
        mgr.settings = broker_settings
        pub = MetricPublisher(mgr)
        assert pub.topic_prefix == broker_settings.topic_prefix
        assert pub.publish_interval == broker_settings.publish_interval


# ---------------------------------------------------------------------------
# publish_metrics() — individual field topics
# ---------------------------------------------------------------------------


class TestPublishMetrics:

    async def test_publishes_all_sensor_topics(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        await publisher.publish_metrics(sample_metrics)

        # One call per _METRIC_FIELDS entry + 1 for state topic
        expected_calls = len(_METRIC_FIELDS) + 1
        assert mock_mqtt.publish.call_count == expected_calls

    async def test_individual_topic_format(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        await publisher.publish_metrics(sample_metrics)

        # Collect all published topics
        topics = [call.args[0] for call in mock_mqtt.publish.call_args_list]

        for field in _METRIC_FIELDS:
            expected_topic = f"casectl/sensor/{field}"
            assert expected_topic in topics, f"Missing topic: {expected_topic}"

    async def test_state_topic_published(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        await publisher.publish_metrics(sample_metrics)

        topics = [call.args[0] for call in mock_mqtt.publish.call_args_list]
        assert "casectl/state" in topics

    async def test_state_topic_is_valid_json(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        await publisher.publish_metrics(sample_metrics)

        # Find the state topic call
        for call in mock_mqtt.publish.call_args_list:
            if call.args[0] == "casectl/state":
                payload = call.args[1]
                parsed = json.loads(payload)
                assert parsed["cpu_percent"] == 42.5
                assert parsed["cpu_temp"] == 58.3
                return

        pytest.fail("State topic not found in publish calls")

    async def test_qos_and_retain_from_settings(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        await publisher.publish_metrics(sample_metrics)

        for call in mock_mqtt.publish.call_args_list:
            assert call.kwargs["qos"] == 1
            assert call.kwargs["retain"] is True

    async def test_publish_count_increments(
        self, publisher: MetricPublisher, sample_metrics: dict
    ) -> None:
        assert publisher.publish_count == 0
        await publisher.publish_metrics(sample_metrics)
        assert publisher.publish_count == 1
        await publisher.publish_metrics(sample_metrics)
        assert publisher.publish_count == 2

    async def test_last_publish_time_updated(
        self, publisher: MetricPublisher, sample_metrics: dict
    ) -> None:
        assert publisher.last_publish_time == 0.0
        before = time.monotonic()
        await publisher.publish_metrics(sample_metrics)
        after = time.monotonic()
        assert before <= publisher.last_publish_time <= after

    async def test_not_connected_raises(
        self, mock_mqtt, sample_metrics: dict
    ) -> None:
        mock_mqtt.is_connected = False
        pub = MetricPublisher(mock_mqtt)
        with pytest.raises(RuntimeError, match="not connected"):
            await pub.publish_metrics(sample_metrics)

    async def test_missing_fields_skipped(
        self, publisher: MetricPublisher, mock_mqtt
    ) -> None:
        """Only fields present in the metrics dict are published."""
        partial_metrics = {"cpu_percent": 50.0, "cpu_temp": 60.0}
        await publisher.publish_metrics(partial_metrics)

        topics = [call.args[0] for call in mock_mqtt.publish.call_args_list]
        assert "casectl/sensor/cpu_percent" in topics
        assert "casectl/sensor/cpu_temp" in topics
        # Fields not in partial_metrics should not be published
        assert "casectl/sensor/memory_percent" not in topics


# ---------------------------------------------------------------------------
# Value serialization
# ---------------------------------------------------------------------------


class TestSerializeValue:

    def test_float_rounded(self) -> None:
        assert MetricPublisher._serialize_value(42.567) == "42.6"

    def test_float_exact(self) -> None:
        assert MetricPublisher._serialize_value(42.0) == "42.0"

    def test_int(self) -> None:
        assert MetricPublisher._serialize_value(128) == "128"

    def test_string(self) -> None:
        assert MetricPublisher._serialize_value("192.168.1.1") == "192.168.1.1"

    def test_list(self) -> None:
        result = MetricPublisher._serialize_value([128, 128, 128])
        assert json.loads(result) == [128, 128, 128]

    def test_dict(self) -> None:
        result = MetricPublisher._serialize_value({"a": 1})
        assert json.loads(result) == {"a": 1}

    def test_empty_list(self) -> None:
        result = MetricPublisher._serialize_value([])
        assert json.loads(result) == []


# ---------------------------------------------------------------------------
# Custom topic prefix
# ---------------------------------------------------------------------------


class TestCustomPrefix:

    async def test_custom_prefix_applied(
        self, mock_mqtt, sample_metrics: dict
    ) -> None:
        pub = MetricPublisher(mock_mqtt, topic_prefix="home/pi")
        await pub.publish_metrics(sample_metrics)

        topics = [call.args[0] for call in mock_mqtt.publish.call_args_list]
        assert "home/pi/sensor/cpu_percent" in topics
        assert "home/pi/state" in topics


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------


class TestEventBusIntegration:

    async def test_metrics_cached_from_event(
        self, publisher: MetricPublisher, mock_event_bus, sample_metrics: dict
    ) -> None:
        """Starting the publisher subscribes to metrics_updated."""
        await publisher.start()

        # Emit metrics_updated event
        await mock_event_bus.emit("metrics_updated", sample_metrics)
        # Allow event handler to execute
        await asyncio.sleep(0)

        assert publisher.latest_metrics is not None
        assert publisher.latest_metrics["cpu_percent"] == 42.5

        await publisher.stop()

    async def test_stop_unsubscribes_from_event_bus(
        self, publisher: MetricPublisher, mock_event_bus, sample_metrics: dict
    ) -> None:
        await publisher.start()
        await publisher.stop()

        # After stopping, emitting should not update cached metrics
        publisher._latest_metrics = None
        await mock_event_bus.emit("metrics_updated", sample_metrics)
        await asyncio.sleep(0)

        assert publisher.latest_metrics is None


# ---------------------------------------------------------------------------
# Background publish loop
# ---------------------------------------------------------------------------


class TestPublishLoop:

    async def test_loop_publishes_periodically(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        publisher._latest_metrics = sample_metrics

        await publisher.start()
        assert publisher.is_running is True

        # Wait for at least one publish cycle
        await asyncio.sleep(0.15)

        await publisher.stop()
        assert publisher.is_running is False
        assert publisher.publish_count >= 1
        assert mock_mqtt.publish.call_count >= len(_METRIC_FIELDS) + 1

    async def test_loop_skips_when_no_metrics(
        self, publisher: MetricPublisher, mock_mqtt
    ) -> None:
        """The loop should skip publishing when no metrics are cached."""
        await publisher.start()
        await asyncio.sleep(0.15)
        await publisher.stop()

        # No metrics cached → no publishes
        assert publisher.publish_count == 0
        mock_mqtt.publish.assert_not_called()

    async def test_loop_skips_when_not_connected(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        publisher._latest_metrics = sample_metrics
        mock_mqtt.is_connected = False

        await publisher.start()
        await asyncio.sleep(0.15)
        await publisher.stop()

        assert publisher.publish_count == 0

    async def test_loop_handles_publish_error(
        self, publisher: MetricPublisher, mock_mqtt, sample_metrics: dict
    ) -> None:
        publisher._latest_metrics = sample_metrics
        mock_mqtt.publish.side_effect = OSError("broken pipe")

        await publisher.start()
        await asyncio.sleep(0.15)
        await publisher.stop()

        assert publisher.error_count >= 1
        assert publisher.publish_count == 0

    async def test_start_idempotent(
        self, publisher: MetricPublisher
    ) -> None:
        await publisher.start()
        task1 = publisher._publish_task
        await publisher.start()  # Should be no-op
        assert publisher._publish_task is task1
        await publisher.stop()

    async def test_stop_idempotent(
        self, publisher: MetricPublisher
    ) -> None:
        """Stop on a non-started publisher should be a no-op."""
        await publisher.stop()
        assert publisher.is_running is False


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:

    def test_initial_status(self, publisher: MetricPublisher) -> None:
        status = publisher.get_status()
        assert status["running"] is False
        assert status["publish_count"] == 0
        assert status["error_count"] == 0
        assert status["publish_interval"] == 0.05
        assert status["topic_prefix"] == "casectl"
        assert status["has_cached_metrics"] is False

    async def test_status_after_publish(
        self, publisher: MetricPublisher, sample_metrics: dict
    ) -> None:
        await publisher.publish_metrics(sample_metrics)
        status = publisher.get_status()
        assert status["publish_count"] == 1
        assert status["has_cached_metrics"] is False  # publish_metrics doesn't cache

    async def test_status_while_running(
        self, publisher: MetricPublisher, sample_metrics: dict
    ) -> None:
        publisher._latest_metrics = sample_metrics
        await publisher.start()
        status = publisher.get_status()
        assert status["running"] is True
        assert status["has_cached_metrics"] is True
        await publisher.stop()


# ---------------------------------------------------------------------------
# MqttConfig publish_interval field
# ---------------------------------------------------------------------------


class TestMqttConfigPublishInterval:

    def test_default_publish_interval(self) -> None:
        from casectl.config.models import MqttConfig

        cfg = MqttConfig()
        assert cfg.publish_interval == 10.0

    def test_custom_publish_interval(self) -> None:
        from casectl.config.models import MqttConfig

        cfg = MqttConfig(publish_interval=30.0)
        assert cfg.publish_interval == 30.0

    def test_publish_interval_min_validation(self) -> None:
        from casectl.config.models import MqttConfig

        with pytest.raises(ValueError):
            MqttConfig(publish_interval=0.5)  # Below minimum of 1.0

    def test_publish_interval_max_validation(self) -> None:
        from casectl.config.models import MqttConfig

        with pytest.raises(ValueError):
            MqttConfig(publish_interval=400.0)  # Above maximum of 300.0

    def test_broker_settings_from_config_includes_interval(self) -> None:
        from casectl.config.models import MqttConfig

        cfg = MqttConfig(publish_interval=15.0)
        settings = BrokerSettings.from_config(cfg)
        assert settings.publish_interval == 15.0


# ---------------------------------------------------------------------------
# _METRIC_FIELDS constant
# ---------------------------------------------------------------------------


class TestMetricFields:

    def test_all_expected_fields_present(self) -> None:
        expected = {
            "cpu_percent",
            "memory_percent",
            "disk_percent",
            "cpu_temp",
            "case_temp",
            "ip_address",
            "fan_duty",
            "motor_speed",
            "rpi_fan_duty",
        }
        assert set(_METRIC_FIELDS) == expected

    def test_fields_match_system_metrics(self) -> None:
        """All _METRIC_FIELDS should be valid SystemMetrics fields."""
        from casectl.config.models import SystemMetrics

        model_fields = set(SystemMetrics.model_fields.keys())
        for field in _METRIC_FIELDS:
            assert field in model_fields, f"{field} not in SystemMetrics"
