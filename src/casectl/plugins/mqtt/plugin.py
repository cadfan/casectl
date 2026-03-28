"""MQTT integration plugin for casectl.

Wires together the :class:`MqttConnectionManager`, :class:`DeviceStateManager`,
:class:`MetricPublisher`, and :class:`HADiscoveryManager` into a single plugin
that integrates with the casectl daemon lifecycle.

The plugin:

* Connects to the MQTT broker on start and disconnects on stop.
* Publishes device state (fans, LEDs, OLED) to retained MQTT topics whenever
  state changes are emitted on the event bus.
* Subscribes to ``{prefix}/{device}/{attr}/set`` command topics so that
  external MQTT clients (including Home Assistant) can control devices.
* Publishes system metrics at a configurable interval.
* Publishes Home Assistant auto-discovery config on connect.
"""

from __future__ import annotations

import logging
from typing import Any

from casectl.plugins.base import PluginContext, PluginStatus
from casectl.plugins.mqtt.client import BrokerSettings, ConnectionState, MqttConnectionManager
from casectl.plugins.mqtt.ha_discovery import HADiscoveryManager
from casectl.plugins.mqtt.metrics import MetricPublisher
from casectl.plugins.mqtt.state import DeviceStateManager

logger = logging.getLogger(__name__)


class MqttPlugin:
    """MQTT integration plugin providing bidirectional device control.

    Connects to a user-configured MQTT broker, publishes device state for
    fans, LEDs, and OLED, and subscribes to command topics for full remote
    control with Home Assistant auto-discovery.
    """

    name: str = "mqtt"
    version: str = "0.2.0"
    description: str = "Bidirectional MQTT integration with Home Assistant auto-discovery"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._mqtt: MqttConnectionManager | None = None
        self._state_manager: DeviceStateManager | None = None
        self._metric_publisher: MetricPublisher | None = None
        self._ha_discovery: HADiscoveryManager | None = None
        self._enabled: bool = False

    # -- lifecycle ----------------------------------------------------------

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes and read MQTT configuration.

        Parameters
        ----------
        ctx:
            The plugin context provided by the plugin host.
        """
        self._ctx = ctx

        # Register REST API routes for MQTT status
        from casectl.plugins.mqtt.routes import router

        ctx.register_routes(router)

        logger.info("MQTT plugin setup complete")

    async def start(self) -> None:
        """Connect to the MQTT broker and start publishing / subscribing.

        Reads MQTT configuration from the config manager.  If MQTT is not
        enabled in config, the plugin remains dormant.
        """
        if self._ctx is None:
            logger.error("MQTT plugin not set up — cannot start")
            return

        # Read MQTT config section
        config = await self._get_mqtt_config()
        if not config.get("enabled", False):
            logger.info("MQTT is disabled in config — plugin dormant")
            return

        self._enabled = True

        # Build broker settings
        settings = self._build_settings(config)
        self._mqtt = MqttConnectionManager(settings)

        # Register state change listener to handle reconnection
        self._mqtt.on_state_change(self._on_connection_state_change)

        # Create sub-components
        config_mgr = self._ctx.config_manager
        event_bus = getattr(self._ctx, "_event_bus", None)

        self._state_manager = DeviceStateManager(
            self._mqtt,
            config_manager=config_mgr,
            event_bus=event_bus,
            topic_prefix=settings.topic_prefix,
        )

        self._metric_publisher = MetricPublisher(
            self._mqtt,
            event_bus=event_bus,
            topic_prefix=settings.topic_prefix,
            publish_interval=settings.publish_interval,
        )

        # Build HA discovery with command entities from state manager
        command_entities = self._state_manager.get_command_entities()
        from casectl.plugins.mqtt.ha_discovery import DEFAULT_ENTITIES

        all_entities = (*DEFAULT_ENTITIES, *command_entities)
        self._ha_discovery = HADiscoveryManager(
            self._mqtt,
            entities=all_entities,
            ha_discovery_prefix=settings.ha_discovery_prefix,
            topic_prefix=settings.topic_prefix,
        )

        # Store references on app.state for route handlers
        self._ctx.set_app_state("mqtt_manager", self._mqtt)
        self._ctx.set_app_state("mqtt_state_manager", self._state_manager)
        self._ctx.set_app_state("mqtt_metric_publisher", self._metric_publisher)
        self._ctx.set_app_state("mqtt_ha_discovery", self._ha_discovery)

        # Connect to broker
        try:
            await self._mqtt.connect()
        except (ConnectionError, ImportError) as exc:
            logger.error("Failed to connect to MQTT broker: %s", exc)
            return

        # Start sub-components
        await self._state_manager.start()
        await self._metric_publisher.start()

        # Publish HA discovery
        try:
            await self._ha_discovery.publish_discovery()
        except RuntimeError:
            logger.warning("Failed to publish HA discovery configs")

        logger.info(
            "MQTT plugin started (broker=%s:%d, prefix=%s)",
            settings.host,
            settings.port,
            settings.topic_prefix,
        )

    async def stop(self) -> None:
        """Stop all MQTT sub-components and disconnect from the broker."""
        if self._metric_publisher is not None:
            await self._metric_publisher.stop()

        if self._state_manager is not None:
            await self._state_manager.stop()

        if self._ha_discovery is not None and self._mqtt is not None and self._mqtt.is_connected:
            try:
                await self._ha_discovery.remove_discovery()
            except RuntimeError:
                logger.debug("Could not remove HA discovery on shutdown")

        if self._mqtt is not None:
            await self._mqtt.disconnect()

        logger.info("MQTT plugin stopped")

    def get_status(self) -> dict[str, Any]:
        """Return plugin health and diagnostic information."""
        if not self._enabled:
            return {
                "status": PluginStatus.STOPPED,
                "enabled": False,
            }

        connection_status = "disconnected"
        if self._mqtt is not None:
            connection_status = self._mqtt.state.value

        status = PluginStatus.HEALTHY if connection_status == "connected" else PluginStatus.DEGRADED

        result: dict[str, Any] = {
            "status": status,
            "enabled": True,
            "connection": connection_status,
        }

        if self._mqtt is not None:
            result["broker"] = self._mqtt.get_status()

        if self._state_manager is not None:
            result["state_manager"] = self._state_manager.get_status()

        if self._metric_publisher is not None:
            result["metric_publisher"] = self._metric_publisher.get_status()

        if self._ha_discovery is not None:
            result["ha_discovery"] = self._ha_discovery.get_status()

        return result

    # -- internal helpers ---------------------------------------------------

    async def _get_mqtt_config(self) -> dict[str, Any]:
        """Read the MQTT configuration from the config manager."""
        if self._ctx is None or self._ctx.config_manager is None:
            return {}

        try:
            raw = await self._ctx.config_manager.get("mqtt")
            if isinstance(raw, dict):
                return raw
            # Pydantic model — dump it
            if hasattr(raw, "model_dump"):
                return raw.model_dump()
            return {}
        except Exception:
            logger.debug("Could not read MQTT config — using defaults")
            return {}

    @staticmethod
    def _build_settings(config: dict[str, Any]) -> BrokerSettings:
        """Build a BrokerSettings from a config dict."""
        return BrokerSettings(
            host=config.get("broker_host", "localhost"),
            port=config.get("broker_port", 1883),
            username=config.get("username", ""),
            password=config.get("password", ""),
            client_id=config.get("client_id", "casectl"),
            topic_prefix=config.get("topic_prefix", "casectl"),
            ha_discovery_prefix=config.get("ha_discovery_prefix", "homeassistant"),
            qos=config.get("qos", 1),
            retain=config.get("retain", True),
            keepalive=config.get("keepalive", 60),
            reconnect_min_delay=config.get("reconnect_min_delay", 1.0),
            reconnect_max_delay=config.get("reconnect_max_delay", 60.0),
            tls_enabled=config.get("tls_enabled", False),
            tls_ca_cert=config.get("tls_ca_cert", ""),
            tls_insecure=config.get("tls_insecure", False),
            birth_topic=config.get("birth_topic", ""),
            will_topic=config.get("will_topic", ""),
            publish_interval=config.get("publish_interval", 10.0),
        )

    def _on_connection_state_change(self, state: ConnectionState) -> None:
        """Handle MQTT connection state changes.

        Re-publishes HA discovery and forces a state re-publish on reconnect.
        """
        if state == ConnectionState.CONNECTED:
            logger.info("MQTT connected — scheduling rediscovery")
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._on_reconnect())
            except RuntimeError:
                pass

    async def _on_reconnect(self) -> None:
        """Re-publish HA discovery and force state re-publish after reconnect."""
        if self._ha_discovery is not None:
            try:
                await self._ha_discovery.publish_discovery()
            except Exception:
                logger.debug("Failed to re-publish HA discovery on reconnect")
