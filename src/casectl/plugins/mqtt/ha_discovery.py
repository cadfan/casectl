"""Home Assistant MQTT auto-discovery for casectl.

Publishes `config` messages to the HA MQTT discovery prefix so that
casectl sensors, fans, and controls appear automatically in Home Assistant
without any manual YAML configuration.

Discovery topic layout::

    homeassistant/sensor/casectl/cpu_temp/config
    homeassistant/sensor/casectl/cpu_percent/config
    homeassistant/sensor/casectl/memory_percent/config
    homeassistant/sensor/casectl/disk_percent/config
    homeassistant/sensor/casectl/case_temp/config
    homeassistant/sensor/casectl/fan_duty_0/config
    homeassistant/sensor/casectl/fan_duty_1/config
    homeassistant/sensor/casectl/fan_duty_2/config
    homeassistant/sensor/casectl/rpi_fan_duty/config
    homeassistant/sensor/casectl/ip_address/config
    homeassistant/binary_sensor/casectl/status/config

Each config payload contains a JSON object following the HA MQTT discovery
schema: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery

All entities reference a shared ``device`` block so they appear grouped
under a single device in the HA device registry.

Reference:
    https://www.home-assistant.io/integrations/mqtt/
    https://www.home-assistant.io/integrations/sensor.mqtt/
    https://www.home-assistant.io/integrations/binary_sensor.mqtt/
"""

from __future__ import annotations

import json
import logging
import platform
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from casectl.plugins.mqtt.client import MqttConnectionManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------

# casectl version — imported lazily to avoid circular imports
_CASECTL_VERSION: str | None = None


def _get_version() -> str:
    """Return the casectl package version string."""
    global _CASECTL_VERSION
    if _CASECTL_VERSION is None:
        try:
            from casectl import __version__

            _CASECTL_VERSION = __version__
        except (ImportError, AttributeError):
            _CASECTL_VERSION = "0.0.0"
    return _CASECTL_VERSION


@dataclass(frozen=True)
class DeviceInfo:
    """Home Assistant device registration block.

    All entities published by casectl share a single device so they are
    grouped together in the HA device registry.

    Parameters
    ----------
    device_id:
        Unique device identifier (defaults to the MQTT ``client_id``).
    name:
        Human-readable device name shown in HA.
    model:
        Device model string (e.g. ``"Freenove FNK0107B"``).
    manufacturer:
        Manufacturer name.
    sw_version:
        Software version (auto-detected from package).
    hw_version:
        Hardware/platform version (auto-detected from ``platform.machine()``).
    """

    device_id: str = "casectl"
    name: str = "casectl"
    model: str = "Freenove FNK0107B"
    manufacturer: str = "casectl"
    sw_version: str = ""
    hw_version: str = ""

    def to_ha_dict(self) -> dict[str, Any]:
        """Serialize to the HA MQTT discovery ``device`` block.

        Returns
        -------
        dict
            A dict suitable for embedding as the ``"device"`` key in a
            discovery config payload.
        """
        sw = self.sw_version or _get_version()
        hw = self.hw_version or platform.machine()
        return {
            "identifiers": [self.device_id],
            "name": self.name,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "sw_version": sw,
            "hw_version": hw,
        }


# ---------------------------------------------------------------------------
# Entity definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityDefinition:
    """Describes a single HA entity to be published via MQTT discovery.

    Parameters
    ----------
    component:
        HA component type (``"sensor"``, ``"binary_sensor"``, ``"number"``, etc.).
    object_id:
        Unique object ID within this device (e.g. ``"cpu_temp"``).
    name:
        Human-readable entity name shown in HA.
    state_topic_suffix:
        Appended to ``{topic_prefix}/`` to form the state topic.
    icon:
        MDI icon string (e.g. ``"mdi:thermometer"``).
    device_class:
        HA device class (e.g. ``"temperature"``, ``"humidity"``).
    state_class:
        HA state class (``"measurement"``, ``"total_increasing"``, etc.).
    unit_of_measurement:
        Unit string (e.g. ``"°C"``, ``"%"``).
    value_template:
        Jinja2 template for extracting the value from the state payload.
    payload_on:
        Payload representing "on" for binary sensors.
    payload_off:
        Payload representing "off" for binary sensors.
    enabled_by_default:
        Whether this entity is enabled by default in HA.
    entity_category:
        HA entity category (``"config"``, ``"diagnostic"``, or ``None``).
    extra:
        Additional keys merged into the discovery config payload.
    """

    component: str = "sensor"
    object_id: str = ""
    name: str = ""
    state_topic_suffix: str = ""
    icon: str = ""
    device_class: str = ""
    state_class: str = ""
    unit_of_measurement: str = ""
    value_template: str = ""
    payload_on: str = ""
    payload_off: str = ""
    enabled_by_default: bool = True
    entity_category: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Built-in entity catalogue
# ---------------------------------------------------------------------------

SENSOR_ENTITIES: tuple[EntityDefinition, ...] = (
    EntityDefinition(
        component="sensor",
        object_id="cpu_temp",
        name="CPU Temperature",
        state_topic_suffix="sensor/cpu_temp",
        icon="mdi:thermometer",
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
    ),
    EntityDefinition(
        component="sensor",
        object_id="case_temp",
        name="Case Temperature",
        state_topic_suffix="sensor/case_temp",
        icon="mdi:thermometer-low",
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
    ),
    EntityDefinition(
        component="sensor",
        object_id="cpu_percent",
        name="CPU Usage",
        state_topic_suffix="sensor/cpu_percent",
        icon="mdi:cpu-64-bit",
        state_class="measurement",
        unit_of_measurement="%",
    ),
    EntityDefinition(
        component="sensor",
        object_id="memory_percent",
        name="Memory Usage",
        state_topic_suffix="sensor/memory_percent",
        icon="mdi:memory",
        state_class="measurement",
        unit_of_measurement="%",
    ),
    EntityDefinition(
        component="sensor",
        object_id="disk_percent",
        name="Disk Usage",
        state_topic_suffix="sensor/disk_percent",
        icon="mdi:harddisk",
        state_class="measurement",
        unit_of_measurement="%",
    ),
    EntityDefinition(
        component="sensor",
        object_id="fan_duty_0",
        name="Fan 1 Duty",
        state_topic_suffix="sensor/fan_duty",
        icon="mdi:fan",
        state_class="measurement",
        unit_of_measurement="",
        value_template="{{ value_json[0] }}",
    ),
    EntityDefinition(
        component="sensor",
        object_id="fan_duty_1",
        name="Fan 2 Duty",
        state_topic_suffix="sensor/fan_duty",
        icon="mdi:fan",
        state_class="measurement",
        unit_of_measurement="",
        value_template="{{ value_json[1] }}",
    ),
    EntityDefinition(
        component="sensor",
        object_id="fan_duty_2",
        name="Fan 3 Duty",
        state_topic_suffix="sensor/fan_duty",
        icon="mdi:fan",
        state_class="measurement",
        unit_of_measurement="",
        value_template="{{ value_json[2] }}",
    ),
    EntityDefinition(
        component="sensor",
        object_id="rpi_fan_duty",
        name="RPi Fan Duty",
        state_topic_suffix="sensor/rpi_fan_duty",
        icon="mdi:fan",
        state_class="measurement",
        unit_of_measurement="",
    ),
    EntityDefinition(
        component="sensor",
        object_id="ip_address",
        name="IP Address",
        state_topic_suffix="sensor/ip_address",
        icon="mdi:ip-network",
        entity_category="diagnostic",
    ),
)

BINARY_SENSOR_ENTITIES: tuple[EntityDefinition, ...] = (
    EntityDefinition(
        component="binary_sensor",
        object_id="status",
        name="Status",
        state_topic_suffix="status",
        icon="mdi:heart-pulse",
        device_class="connectivity",
        payload_on="online",
        payload_off="offline",
        entity_category="diagnostic",
    ),
)

# Combined catalogue — the default set published on connect.
DEFAULT_ENTITIES: tuple[EntityDefinition, ...] = (
    *SENSOR_ENTITIES,
    *BINARY_SENSOR_ENTITIES,
)


# ---------------------------------------------------------------------------
# Discovery publisher
# ---------------------------------------------------------------------------


class HADiscoveryManager:
    """Manages Home Assistant MQTT auto-discovery for casectl entities.

    On :meth:`publish_discovery` the manager publishes a retained config
    message for every entity in the catalogue.  On :meth:`remove_discovery`
    it publishes empty payloads to the same topics, which causes HA to
    remove the entities.

    Parameters
    ----------
    mqtt_manager:
        A connected :class:`MqttConnectionManager`.
    device_info:
        Optional :class:`DeviceInfo` override.  If ``None``, a default
        device is created from the MQTT client ID.
    entities:
        Tuple of :class:`EntityDefinition` objects.  Defaults to
        :data:`DEFAULT_ENTITIES`.
    ha_discovery_prefix:
        Override for the HA discovery prefix.  Defaults to the broker
        setting's ``ha_discovery_prefix``.
    topic_prefix:
        Override for the casectl topic prefix.  Defaults to the broker
        setting's ``topic_prefix``.
    """

    def __init__(
        self,
        mqtt_manager: MqttConnectionManager,
        *,
        device_info: DeviceInfo | None = None,
        entities: tuple[EntityDefinition, ...] = DEFAULT_ENTITIES,
        ha_discovery_prefix: str | None = None,
        topic_prefix: str | None = None,
    ) -> None:
        self._mqtt = mqtt_manager
        self._entities = entities
        self._ha_prefix = (
            ha_discovery_prefix or mqtt_manager.settings.ha_discovery_prefix
        )
        self._topic_prefix = topic_prefix or mqtt_manager.settings.topic_prefix
        self._device_info = device_info or DeviceInfo(
            device_id=mqtt_manager.settings.client_id,
            name="casectl",
        )
        self._published: bool = False
        self._publish_count: int = 0

    # -- properties ---------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        """The device registration info used for discovery."""
        return self._device_info

    @property
    def entities(self) -> tuple[EntityDefinition, ...]:
        """The entity catalogue being managed."""
        return self._entities

    @property
    def ha_discovery_prefix(self) -> str:
        """The HA MQTT discovery prefix."""
        return self._ha_prefix

    @property
    def topic_prefix(self) -> str:
        """The casectl MQTT topic prefix."""
        return self._topic_prefix

    @property
    def is_published(self) -> bool:
        """Whether discovery configs have been published."""
        return self._published

    @property
    def publish_count(self) -> int:
        """Number of discovery publish cycles completed."""
        return self._publish_count

    # -- discovery topic helpers --------------------------------------------

    def _discovery_topic(self, entity: EntityDefinition) -> str:
        """Build the HA discovery config topic for an entity.

        Format: ``{ha_prefix}/{component}/{device_id}/{object_id}/config``
        """
        return (
            f"{self._ha_prefix}/{entity.component}/"
            f"{self._device_info.device_id}/{entity.object_id}/config"
        )

    def _build_config_payload(self, entity: EntityDefinition) -> dict[str, Any]:
        """Build the HA discovery config payload dict for an entity.

        Returns
        -------
        dict
            JSON-serialisable HA discovery config.
        """
        device_id = self._device_info.device_id
        unique_id = f"{device_id}_{entity.object_id}"
        state_topic = f"{self._topic_prefix}/{entity.state_topic_suffix}"
        availability_topic = f"{self._topic_prefix}/status"

        payload: dict[str, Any] = {
            "name": entity.name,
            "unique_id": unique_id,
            "object_id": f"{device_id}_{entity.object_id}",
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self._device_info.to_ha_dict(),
        }

        # Optional fields — only include if set
        if entity.icon:
            payload["icon"] = entity.icon
        if entity.device_class:
            payload["device_class"] = entity.device_class
        if entity.state_class:
            payload["state_class"] = entity.state_class
        if entity.unit_of_measurement:
            payload["unit_of_measurement"] = entity.unit_of_measurement
        if entity.value_template:
            payload["value_template"] = entity.value_template
        if entity.payload_on:
            payload["payload_on"] = entity.payload_on
        if entity.payload_off:
            payload["payload_off"] = entity.payload_off
        if not entity.enabled_by_default:
            payload["enabled_by_default"] = False
        if entity.entity_category:
            payload["entity_category"] = entity.entity_category

        # Merge any extra keys
        if entity.extra:
            payload.update(entity.extra)

        return payload

    # -- public API ---------------------------------------------------------

    def build_discovery_payloads(
        self,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Build all discovery (topic, payload) pairs without publishing.

        Useful for testing or dry-run inspection.

        Returns
        -------
        list of (topic, payload_dict) tuples
        """
        results: list[tuple[str, dict[str, Any]]] = []
        for entity in self._entities:
            topic = self._discovery_topic(entity)
            payload = self._build_config_payload(entity)
            results.append((topic, payload))
        return results

    async def publish_discovery(self) -> int:
        """Publish HA discovery config messages for all entities.

        Each message is published with QoS 1 and the retain flag set,
        ensuring HA picks up entities even if it restarts after casectl.

        Returns
        -------
        int
            Number of config messages published.

        Raises
        ------
        RuntimeError
            If the MQTT manager is not connected.
        """
        if not self._mqtt.is_connected:
            raise RuntimeError("MQTT client is not connected")

        count = 0
        for entity in self._entities:
            topic = self._discovery_topic(entity)
            payload = self._build_config_payload(entity)
            payload_json = json.dumps(payload, separators=(",", ":"))

            try:
                await self._mqtt.publish(
                    topic,
                    payload_json,
                    qos=self._mqtt.settings.qos,
                    retain=True,
                )
                count += 1
                logger.debug("Published HA discovery: %s", topic)
            except Exception:
                logger.warning("Failed to publish HA discovery: %s", topic)

        self._published = count > 0
        self._publish_count += 1
        logger.info(
            "Published %d HA discovery configs (cycle #%d)",
            count,
            self._publish_count,
        )
        return count

    async def remove_discovery(self) -> int:
        """Remove all HA discovery entities by publishing empty payloads.

        This causes Home Assistant to remove the entities from the registry.

        Returns
        -------
        int
            Number of removal messages published.

        Raises
        ------
        RuntimeError
            If the MQTT manager is not connected.
        """
        if not self._mqtt.is_connected:
            raise RuntimeError("MQTT client is not connected")

        count = 0
        for entity in self._entities:
            topic = self._discovery_topic(entity)
            try:
                await self._mqtt.publish(
                    topic,
                    "",
                    qos=self._mqtt.settings.qos,
                    retain=True,
                )
                count += 1
                logger.debug("Removed HA discovery: %s", topic)
            except Exception:
                logger.warning("Failed to remove HA discovery: %s", topic)

        self._published = False
        logger.info("Removed %d HA discovery configs", count)
        return count

    def get_status(self) -> dict[str, Any]:
        """Return diagnostic information about discovery state.

        Returns
        -------
        dict
            Keys: ``published``, ``entity_count``, ``publish_count``,
            ``ha_discovery_prefix``, ``device_id``.
        """
        return {
            "published": self._published,
            "entity_count": len(self._entities),
            "publish_count": self._publish_count,
            "ha_discovery_prefix": self._ha_prefix,
            "device_id": self._device_info.device_id,
        }
