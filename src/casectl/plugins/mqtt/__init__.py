"""MQTT integration plugin for casectl.

Provides bidirectional MQTT communication with configurable broker settings,
QoS 1 defaults, reconnection logic, retained messages, and Home Assistant
auto-discovery support.
"""

from casectl.plugins.mqtt.client import MqttConnectionManager
from casectl.plugins.mqtt.ha_discovery import (
    DEFAULT_ENTITIES,
    DeviceInfo,
    EntityDefinition,
    HADiscoveryManager,
)
from casectl.plugins.mqtt.metrics import MetricPublisher
from casectl.plugins.mqtt.plugin import MqttPlugin
from casectl.plugins.mqtt.state import MAX_CURVE_POINTS, DeviceStateManager

__all__ = [
    "DEFAULT_ENTITIES",
    "DeviceInfo",
    "DeviceStateManager",
    "MAX_CURVE_POINTS",
    "EntityDefinition",
    "HADiscoveryManager",
    "MetricPublisher",
    "MqttConnectionManager",
    "MqttPlugin",
]
