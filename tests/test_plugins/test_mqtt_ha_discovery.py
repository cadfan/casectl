"""Tests for Home Assistant MQTT auto-discovery.

All tests mock the MQTT connection manager so no real broker is needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, PropertyMock

import pytest

from casectl.plugins.mqtt.ha_discovery import (
    BINARY_SENSOR_ENTITIES,
    DEFAULT_ENTITIES,
    SENSOR_ENTITIES,
    DeviceInfo,
    EntityDefinition,
    HADiscoveryManager,
    _get_version,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_mqtt() -> AsyncMock:
    """Return a mock MqttConnectionManager that appears connected."""
    mgr = AsyncMock()
    type(mgr).is_connected = PropertyMock(return_value=True)
    mgr.settings = AsyncMock()
    mgr.settings.ha_discovery_prefix = "homeassistant"
    mgr.settings.topic_prefix = "casectl"
    mgr.settings.client_id = "casectl"
    mgr.settings.qos = 1
    mgr.settings.retain = True
    mgr.publish = AsyncMock()
    return mgr


@pytest.fixture()
def device_info() -> DeviceInfo:
    """Return a default DeviceInfo."""
    return DeviceInfo(device_id="casectl-test", name="casectl Test")


@pytest.fixture()
def discovery(mock_mqtt: AsyncMock, device_info: DeviceInfo) -> HADiscoveryManager:
    """Return an HADiscoveryManager with default entities."""
    return HADiscoveryManager(mock_mqtt, device_info=device_info)


# ---------------------------------------------------------------------------
# DeviceInfo tests
# ---------------------------------------------------------------------------


class TestDeviceInfo:

    def test_default_values(self) -> None:
        d = DeviceInfo()
        assert d.device_id == "casectl"
        assert d.name == "casectl"
        assert d.model == "Freenove FNK0107B"
        assert d.manufacturer == "casectl"

    def test_to_ha_dict_keys(self) -> None:
        d = DeviceInfo(device_id="test-id", name="Test Device")
        result = d.to_ha_dict()
        assert result["identifiers"] == ["test-id"]
        assert result["name"] == "Test Device"
        assert result["model"] == "Freenove FNK0107B"
        assert result["manufacturer"] == "casectl"
        assert "sw_version" in result
        assert "hw_version" in result

    def test_custom_sw_hw_version(self) -> None:
        d = DeviceInfo(sw_version="1.2.3", hw_version="aarch64")
        result = d.to_ha_dict()
        assert result["sw_version"] == "1.2.3"
        assert result["hw_version"] == "aarch64"

    def test_frozen(self) -> None:
        d = DeviceInfo()
        with pytest.raises(AttributeError):
            d.device_id = "other"  # type: ignore[misc]

    def test_auto_detects_version(self) -> None:
        d = DeviceInfo()
        result = d.to_ha_dict()
        # Should have a non-empty sw_version (auto-detected or fallback)
        assert isinstance(result["sw_version"], str)
        assert len(result["sw_version"]) > 0


# ---------------------------------------------------------------------------
# EntityDefinition tests
# ---------------------------------------------------------------------------


class TestEntityDefinition:

    def test_default_values(self) -> None:
        e = EntityDefinition()
        assert e.component == "sensor"
        assert e.object_id == ""
        assert e.enabled_by_default is True

    def test_custom_entity(self) -> None:
        e = EntityDefinition(
            component="binary_sensor",
            object_id="status",
            name="Status",
            device_class="connectivity",
            payload_on="online",
            payload_off="offline",
        )
        assert e.component == "binary_sensor"
        assert e.payload_on == "online"

    def test_frozen(self) -> None:
        e = EntityDefinition(object_id="test")
        with pytest.raises(AttributeError):
            e.object_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Entity catalogue tests
# ---------------------------------------------------------------------------


class TestEntityCatalogue:

    def test_sensor_entities_count(self) -> None:
        assert len(SENSOR_ENTITIES) == 10

    def test_binary_sensor_entities_count(self) -> None:
        assert len(BINARY_SENSOR_ENTITIES) == 1

    def test_default_entities_is_combined(self) -> None:
        assert len(DEFAULT_ENTITIES) == len(SENSOR_ENTITIES) + len(BINARY_SENSOR_ENTITIES)

    def test_all_entities_have_object_id(self) -> None:
        for entity in DEFAULT_ENTITIES:
            assert entity.object_id, f"Entity missing object_id: {entity}"

    def test_all_entities_have_name(self) -> None:
        for entity in DEFAULT_ENTITIES:
            assert entity.name, f"Entity missing name: {entity}"

    def test_all_entities_have_state_topic_suffix(self) -> None:
        for entity in DEFAULT_ENTITIES:
            assert entity.state_topic_suffix, f"Entity missing state_topic_suffix: {entity}"

    def test_unique_object_ids(self) -> None:
        ids = [e.object_id for e in DEFAULT_ENTITIES]
        assert len(ids) == len(set(ids)), f"Duplicate object_ids found: {ids}"

    def test_sensor_components(self) -> None:
        for entity in SENSOR_ENTITIES:
            assert entity.component == "sensor"

    def test_binary_sensor_components(self) -> None:
        for entity in BINARY_SENSOR_ENTITIES:
            assert entity.component == "binary_sensor"

    def test_cpu_temp_entity(self) -> None:
        cpu_temp = next(e for e in SENSOR_ENTITIES if e.object_id == "cpu_temp")
        assert cpu_temp.device_class == "temperature"
        assert cpu_temp.unit_of_measurement == "°C"
        assert cpu_temp.state_class == "measurement"
        assert cpu_temp.icon == "mdi:thermometer"

    def test_fan_duty_entities_have_value_template(self) -> None:
        fan_entities = [e for e in SENSOR_ENTITIES if e.object_id.startswith("fan_duty_")]
        assert len(fan_entities) == 3
        for i, entity in enumerate(fan_entities):
            assert f"value_json[{i}]" in entity.value_template

    def test_status_binary_sensor(self) -> None:
        status = next(e for e in BINARY_SENSOR_ENTITIES if e.object_id == "status")
        assert status.device_class == "connectivity"
        assert status.payload_on == "online"
        assert status.payload_off == "offline"
        assert status.entity_category == "diagnostic"


# ---------------------------------------------------------------------------
# HADiscoveryManager — properties
# ---------------------------------------------------------------------------


class TestDiscoveryManagerProperties:

    def test_initial_state(self, discovery: HADiscoveryManager) -> None:
        assert discovery.is_published is False
        assert discovery.publish_count == 0

    def test_device_info(self, discovery: HADiscoveryManager, device_info: DeviceInfo) -> None:
        assert discovery.device_info is device_info

    def test_entities(self, discovery: HADiscoveryManager) -> None:
        assert discovery.entities == DEFAULT_ENTITIES

    def test_ha_discovery_prefix(self, discovery: HADiscoveryManager) -> None:
        assert discovery.ha_discovery_prefix == "homeassistant"

    def test_topic_prefix(self, discovery: HADiscoveryManager) -> None:
        assert discovery.topic_prefix == "casectl"

    def test_custom_entities(self, mock_mqtt: AsyncMock) -> None:
        custom = (
            EntityDefinition(object_id="custom", name="Custom", state_topic_suffix="custom"),
        )
        mgr = HADiscoveryManager(mock_mqtt, entities=custom)
        assert mgr.entities == custom

    def test_custom_prefixes(self, mock_mqtt: AsyncMock) -> None:
        mgr = HADiscoveryManager(
            mock_mqtt,
            ha_discovery_prefix="ha_custom",
            topic_prefix="myctl",
        )
        assert mgr.ha_discovery_prefix == "ha_custom"
        assert mgr.topic_prefix == "myctl"

    def test_default_device_from_client_id(self, mock_mqtt: AsyncMock) -> None:
        """When no device_info is given, it should use the client_id."""
        mgr = HADiscoveryManager(mock_mqtt)
        assert mgr.device_info.device_id == "casectl"


# ---------------------------------------------------------------------------
# HADiscoveryManager — discovery topic building
# ---------------------------------------------------------------------------


class TestDiscoveryTopics:

    def test_sensor_topic_format(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="cpu_temp",
            name="CPU Temp",
            state_topic_suffix="sensor/cpu_temp",
        )
        topic = discovery._discovery_topic(entity)
        assert topic == "homeassistant/sensor/casectl-test/cpu_temp/config"

    def test_binary_sensor_topic_format(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="binary_sensor",
            object_id="status",
            name="Status",
            state_topic_suffix="status",
        )
        topic = discovery._discovery_topic(entity)
        assert topic == "homeassistant/binary_sensor/casectl-test/status/config"


# ---------------------------------------------------------------------------
# HADiscoveryManager — config payload building
# ---------------------------------------------------------------------------


class TestConfigPayloads:

    def test_basic_sensor_payload(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="cpu_temp",
            name="CPU Temperature",
            state_topic_suffix="sensor/cpu_temp",
            icon="mdi:thermometer",
            device_class="temperature",
            state_class="measurement",
            unit_of_measurement="°C",
        )
        payload = discovery._build_config_payload(entity)

        assert payload["name"] == "CPU Temperature"
        assert payload["unique_id"] == "casectl-test_cpu_temp"
        assert payload["object_id"] == "casectl-test_cpu_temp"
        assert payload["state_topic"] == "casectl/sensor/cpu_temp"
        assert payload["availability_topic"] == "casectl/status"
        assert payload["payload_available"] == "online"
        assert payload["payload_not_available"] == "offline"
        assert payload["icon"] == "mdi:thermometer"
        assert payload["device_class"] == "temperature"
        assert payload["state_class"] == "measurement"
        assert payload["unit_of_measurement"] == "°C"
        assert "device" in payload
        assert payload["device"]["identifiers"] == ["casectl-test"]

    def test_binary_sensor_payload(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="binary_sensor",
            object_id="status",
            name="Status",
            state_topic_suffix="status",
            device_class="connectivity",
            payload_on="online",
            payload_off="offline",
        )
        payload = discovery._build_config_payload(entity)

        assert payload["payload_on"] == "online"
        assert payload["payload_off"] == "offline"
        assert payload["device_class"] == "connectivity"

    def test_optional_fields_excluded_when_empty(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="minimal",
            name="Minimal",
            state_topic_suffix="sensor/minimal",
        )
        payload = discovery._build_config_payload(entity)

        assert "icon" not in payload
        assert "device_class" not in payload
        assert "state_class" not in payload
        assert "unit_of_measurement" not in payload
        assert "value_template" not in payload
        assert "payload_on" not in payload
        assert "payload_off" not in payload
        assert "entity_category" not in payload

    def test_value_template_included(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="fan_duty_0",
            name="Fan 1",
            state_topic_suffix="sensor/fan_duty",
            value_template="{{ value_json[0] }}",
        )
        payload = discovery._build_config_payload(entity)
        assert payload["value_template"] == "{{ value_json[0] }}"

    def test_entity_category_included(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="diag",
            name="Diagnostic",
            state_topic_suffix="sensor/diag",
            entity_category="diagnostic",
        )
        payload = discovery._build_config_payload(entity)
        assert payload["entity_category"] == "diagnostic"

    def test_enabled_by_default_false(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="disabled",
            name="Disabled",
            state_topic_suffix="sensor/disabled",
            enabled_by_default=False,
        )
        payload = discovery._build_config_payload(entity)
        assert payload["enabled_by_default"] is False

    def test_enabled_by_default_true_not_in_payload(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="enabled",
            name="Enabled",
            state_topic_suffix="sensor/enabled",
            enabled_by_default=True,
        )
        payload = discovery._build_config_payload(entity)
        assert "enabled_by_default" not in payload

    def test_extra_keys_merged(self, discovery: HADiscoveryManager) -> None:
        entity = EntityDefinition(
            component="sensor",
            object_id="extra",
            name="Extra",
            state_topic_suffix="sensor/extra",
            extra={"force_update": True, "expire_after": 120},
        )
        payload = discovery._build_config_payload(entity)
        assert payload["force_update"] is True
        assert payload["expire_after"] == 120


# ---------------------------------------------------------------------------
# HADiscoveryManager — build_discovery_payloads
# ---------------------------------------------------------------------------


class TestBuildDiscoveryPayloads:

    def test_returns_all_entities(self, discovery: HADiscoveryManager) -> None:
        payloads = discovery.build_discovery_payloads()
        assert len(payloads) == len(DEFAULT_ENTITIES)

    def test_returns_tuples_of_topic_and_dict(self, discovery: HADiscoveryManager) -> None:
        payloads = discovery.build_discovery_payloads()
        for topic, payload in payloads:
            assert isinstance(topic, str)
            assert isinstance(payload, dict)
            assert "config" in topic
            assert "unique_id" in payload
            assert "device" in payload

    def test_all_payloads_are_json_serializable(self, discovery: HADiscoveryManager) -> None:
        payloads = discovery.build_discovery_payloads()
        for _, payload in payloads:
            # Should not raise
            json.dumps(payload)


# ---------------------------------------------------------------------------
# HADiscoveryManager — publish_discovery
# ---------------------------------------------------------------------------


class TestPublishDiscovery:

    async def test_publishes_all_entities(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        count = await discovery.publish_discovery()
        assert count == len(DEFAULT_ENTITIES)
        assert mock_mqtt.publish.call_count == len(DEFAULT_ENTITIES)
        assert discovery.is_published is True
        assert discovery.publish_count == 1

    async def test_publish_uses_retain_true(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        for call in mock_mqtt.publish.call_args_list:
            assert call.kwargs.get("retain") is True or call[2].get("retain") is True

    async def test_publish_uses_qos_from_settings(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        for call in mock_mqtt.publish.call_args_list:
            assert call.kwargs.get("qos") == 1 or call[2].get("qos") == 1

    async def test_publish_payloads_are_json(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        for call in mock_mqtt.publish.call_args_list:
            payload_str = call[0][1]  # second positional arg
            parsed = json.loads(payload_str)
            assert "unique_id" in parsed
            assert "device" in parsed

    async def test_publish_not_connected_raises(self, mock_mqtt: AsyncMock) -> None:
        type(mock_mqtt).is_connected = PropertyMock(return_value=False)
        mgr = HADiscoveryManager(mock_mqtt)
        with pytest.raises(RuntimeError, match="not connected"):
            await mgr.publish_discovery()

    async def test_publish_handles_individual_failures(
        self, mock_mqtt: AsyncMock
    ) -> None:
        """A failure on one entity should not prevent publishing others."""
        call_count = 0

        async def selective_fail(topic, payload, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:  # Fail on the 3rd entity
                raise OSError("publish error")

        mock_mqtt.publish = selective_fail
        mgr = HADiscoveryManager(mock_mqtt, device_info=DeviceInfo())
        count = await mgr.publish_discovery()
        # Should have published all except the one that failed
        assert count == len(DEFAULT_ENTITIES) - 1

    async def test_publish_increments_count(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        assert discovery.publish_count == 1
        await discovery.publish_discovery()
        assert discovery.publish_count == 2

    async def test_discovery_topics_format(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        topics = [call[0][0] for call in mock_mqtt.publish.call_args_list]
        for topic in topics:
            assert topic.startswith("homeassistant/")
            assert topic.endswith("/config")
            parts = topic.split("/")
            assert len(parts) == 5  # prefix/component/device_id/object_id/config


# ---------------------------------------------------------------------------
# HADiscoveryManager — remove_discovery
# ---------------------------------------------------------------------------


class TestRemoveDiscovery:

    async def test_removes_all_entities(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        # First publish, then remove
        await discovery.publish_discovery()
        mock_mqtt.publish.reset_mock()

        count = await discovery.remove_discovery()
        assert count == len(DEFAULT_ENTITIES)
        assert discovery.is_published is False

    async def test_remove_publishes_empty_payloads(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        mock_mqtt.publish.reset_mock()

        await discovery.remove_discovery()
        for call in mock_mqtt.publish.call_args_list:
            payload = call[0][1]  # second positional arg
            assert payload == ""

    async def test_remove_uses_retain_true(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.remove_discovery()
        for call in mock_mqtt.publish.call_args_list:
            assert call.kwargs.get("retain") is True or call[2].get("retain") is True

    async def test_remove_not_connected_raises(self, mock_mqtt: AsyncMock) -> None:
        type(mock_mqtt).is_connected = PropertyMock(return_value=False)
        mgr = HADiscoveryManager(mock_mqtt)
        with pytest.raises(RuntimeError, match="not connected"):
            await mgr.remove_discovery()

    async def test_remove_handles_individual_failures(
        self, mock_mqtt: AsyncMock
    ) -> None:
        call_count = 0

        async def selective_fail(topic, payload, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("publish error")

        mock_mqtt.publish = selective_fail
        mgr = HADiscoveryManager(mock_mqtt, device_info=DeviceInfo())
        count = await mgr.remove_discovery()
        assert count == len(DEFAULT_ENTITIES) - 1


# ---------------------------------------------------------------------------
# HADiscoveryManager — get_status
# ---------------------------------------------------------------------------


class TestDiscoveryStatus:

    def test_initial_status(self, discovery: HADiscoveryManager) -> None:
        status = discovery.get_status()
        assert status["published"] is False
        assert status["entity_count"] == len(DEFAULT_ENTITIES)
        assert status["publish_count"] == 0
        assert status["ha_discovery_prefix"] == "homeassistant"
        assert status["device_id"] == "casectl-test"

    async def test_status_after_publish(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        status = discovery.get_status()
        assert status["published"] is True
        assert status["publish_count"] == 1

    async def test_status_after_remove(
        self, discovery: HADiscoveryManager, mock_mqtt: AsyncMock
    ) -> None:
        await discovery.publish_discovery()
        await discovery.remove_discovery()
        status = discovery.get_status()
        assert status["published"] is False


# ---------------------------------------------------------------------------
# Integration-style tests — verify full catalogue payloads
# ---------------------------------------------------------------------------


class TestFullCatalogueIntegration:
    """Verify the complete default entity catalogue produces valid discovery payloads."""

    def test_all_default_entities_produce_valid_payloads(
        self, discovery: HADiscoveryManager
    ) -> None:
        payloads = discovery.build_discovery_payloads()

        for topic, payload in payloads:
            # Every payload must have the required HA discovery fields
            assert "name" in payload
            assert "unique_id" in payload
            assert "state_topic" in payload
            assert "availability_topic" in payload
            assert "device" in payload

            # Device block must have identifiers
            assert "identifiers" in payload["device"]
            assert len(payload["device"]["identifiers"]) > 0

            # State topic should start with the prefix
            assert payload["state_topic"].startswith("casectl/")

            # Availability topic should be the status topic
            assert payload["availability_topic"] == "casectl/status"

    def test_cpu_temp_full_discovery(self, discovery: HADiscoveryManager) -> None:
        payloads = dict(discovery.build_discovery_payloads())

        cpu_temp_topic = "homeassistant/sensor/casectl-test/cpu_temp/config"
        assert cpu_temp_topic in payloads

        p = payloads[cpu_temp_topic]
        assert p["name"] == "CPU Temperature"
        assert p["device_class"] == "temperature"
        assert p["unit_of_measurement"] == "°C"
        assert p["state_topic"] == "casectl/sensor/cpu_temp"
        assert p["icon"] == "mdi:thermometer"

    def test_status_binary_sensor_full_discovery(
        self, discovery: HADiscoveryManager
    ) -> None:
        payloads = dict(discovery.build_discovery_payloads())

        status_topic = "homeassistant/binary_sensor/casectl-test/status/config"
        assert status_topic in payloads

        p = payloads[status_topic]
        assert p["name"] == "Status"
        assert p["device_class"] == "connectivity"
        assert p["payload_on"] == "online"
        assert p["payload_off"] == "offline"
        assert p["entity_category"] == "diagnostic"

    def test_fan_duty_entities_use_value_templates(
        self, discovery: HADiscoveryManager
    ) -> None:
        payloads = dict(discovery.build_discovery_payloads())

        for i in range(3):
            topic = f"homeassistant/sensor/casectl-test/fan_duty_{i}/config"
            assert topic in payloads
            p = payloads[topic]
            assert p["value_template"] == f"{{{{ value_json[{i}] }}}}"
            assert p["state_topic"] == "casectl/sensor/fan_duty"


# ---------------------------------------------------------------------------
# Version helper
# ---------------------------------------------------------------------------


class TestGetVersion:

    def test_returns_string(self) -> None:
        version = _get_version()
        assert isinstance(version, str)
        assert len(version) > 0
