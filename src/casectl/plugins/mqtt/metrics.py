"""MQTT metric publisher for casectl.

Subscribes to the ``metrics_updated`` event bus event and publishes all
casectl metrics to MQTT topics at a configurable interval.  Each metric
field is published to its own topic under ``{topic_prefix}/sensor/{field}``
with QoS 1 and the retained flag set, ensuring Home Assistant and other
subscribers always have the latest state.

Topic layout::

    casectl/sensor/cpu_percent      → "42.5"
    casectl/sensor/cpu_temp         → "58.3"
    casectl/sensor/memory_percent   → "61.2"
    casectl/sensor/disk_percent     → "34.7"
    casectl/sensor/case_temp        → "31.0"
    casectl/sensor/fan_duty         → "[128, 128, 128]"
    casectl/sensor/motor_speed      → "[1200, 1180, 1190]"
    casectl/sensor/ip_address       → "192.168.1.42"
    casectl/sensor/rpi_fan_duty     → "200"
    casectl/state                   → '{"cpu_percent": 42.5, ...}'

The ``casectl/state`` topic receives a single JSON blob with all metrics
for consumers that prefer a single subscription.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from casectl.daemon.event_bus import EventBus
    from casectl.plugins.mqtt.client import MqttConnectionManager

logger = logging.getLogger(__name__)

# Metric fields that are published as individual topics.
# Lists/complex values are JSON-encoded; scalars are stringified.
_METRIC_FIELDS: tuple[str, ...] = (
    "cpu_percent",
    "memory_percent",
    "disk_percent",
    "cpu_temp",
    "case_temp",
    "ip_address",
    "fan_duty",
    "motor_speed",
    "rpi_fan_duty",
)


class MetricPublisher:
    """Publishes casectl metrics to MQTT topics on a configurable interval.

    The publisher listens for ``metrics_updated`` events on the event bus.
    Each time the event fires the latest metrics snapshot is cached.  A
    background task then publishes all cached metrics to the MQTT broker at
    the configured :attr:`~BrokerSettings.publish_interval`.

    Parameters
    ----------
    mqtt_manager:
        A connected :class:`MqttConnectionManager`.
    event_bus:
        The daemon's :class:`EventBus` instance.  If ``None``, the publisher
        operates in push-only mode — call :meth:`publish_metrics` directly.
    topic_prefix:
        Override for the MQTT topic prefix (defaults to the manager's
        ``settings.topic_prefix``).
    publish_interval:
        Override for the publishing interval in seconds (defaults to the
        manager's ``settings.publish_interval``).
    """

    def __init__(
        self,
        mqtt_manager: MqttConnectionManager,
        event_bus: EventBus | None = None,
        *,
        topic_prefix: str | None = None,
        publish_interval: float | None = None,
    ) -> None:
        self._mqtt = mqtt_manager
        self._event_bus = event_bus
        self._topic_prefix = topic_prefix or mqtt_manager.settings.topic_prefix
        self._publish_interval = (
            publish_interval
            if publish_interval is not None
            else mqtt_manager.settings.publish_interval
        )
        self._latest_metrics: dict[str, Any] | None = None
        self._publish_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_publish_time: float = 0.0
        self._publish_count: int = 0
        self._error_count: int = 0

    # -- properties ---------------------------------------------------------

    @property
    def topic_prefix(self) -> str:
        """The MQTT topic prefix used for metric topics."""
        return self._topic_prefix

    @property
    def publish_interval(self) -> float:
        """Publishing interval in seconds."""
        return self._publish_interval

    @property
    def publish_count(self) -> int:
        """Total number of successful publish cycles completed."""
        return self._publish_count

    @property
    def error_count(self) -> int:
        """Total number of publish errors encountered."""
        return self._error_count

    @property
    def last_publish_time(self) -> float:
        """Monotonic timestamp of the last successful publish cycle."""
        return self._last_publish_time

    @property
    def is_running(self) -> bool:
        """Whether the background publish loop is running."""
        return self._publish_task is not None and not self._publish_task.done()

    @property
    def latest_metrics(self) -> dict[str, Any] | None:
        """The most recently cached metrics snapshot, or ``None``."""
        return self._latest_metrics

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start the background publish loop and subscribe to events.

        Safe to call multiple times — subsequent calls are no-ops if already
        running.
        """
        if self.is_running:
            logger.debug("MetricPublisher already running — ignoring start()")
            return

        self._stop_event.clear()

        # Subscribe to event bus if available
        if self._event_bus is not None:
            self._event_bus.subscribe("metrics_updated", self._on_metrics_updated)

        self._publish_task = asyncio.create_task(
            self._publish_loop(), name="mqtt-metric-publisher"
        )
        logger.info(
            "MetricPublisher started (interval=%.1fs, prefix=%s)",
            self._publish_interval,
            self._topic_prefix,
        )

    async def stop(self) -> None:
        """Stop the background publish loop and unsubscribe from events."""
        self._stop_event.set()

        if self._event_bus is not None:
            self._event_bus.unsubscribe("metrics_updated", self._on_metrics_updated)

        if self._publish_task is not None and not self._publish_task.done():
            self._publish_task.cancel()
            try:
                await self._publish_task
            except asyncio.CancelledError:
                pass

        self._publish_task = None
        logger.info("MetricPublisher stopped")

    # -- event handler ------------------------------------------------------

    async def _on_metrics_updated(self, data: dict[str, Any]) -> None:
        """Cache the latest metrics snapshot from the event bus.

        Parameters
        ----------
        data:
            A dict matching the :class:`SystemMetrics` schema.
        """
        self._latest_metrics = data

    # -- publishing ---------------------------------------------------------

    async def publish_metrics(self, metrics: dict[str, Any]) -> None:
        """Publish a metrics snapshot to MQTT topics.

        Each metric field is published to ``{prefix}/sensor/{field}`` and a
        combined JSON blob is published to ``{prefix}/state``.  All messages
        use QoS 1 and the retained flag from broker settings.

        Parameters
        ----------
        metrics:
            A dict matching the :class:`SystemMetrics` schema.

        Raises
        ------
        RuntimeError
            If the MQTT manager is not connected.
        """
        if not self._mqtt.is_connected:
            raise RuntimeError("MQTT client is not connected")

        qos = self._mqtt.settings.qos
        retain = self._mqtt.settings.retain

        # Publish individual sensor topics
        for field in _METRIC_FIELDS:
            if field not in metrics:
                continue
            value = metrics[field]
            topic = f"{self._topic_prefix}/sensor/{field}"
            payload = self._serialize_value(value)
            await self._mqtt.publish(topic, payload, qos=qos, retain=retain)

        # Publish combined state topic
        state_topic = f"{self._topic_prefix}/state"
        state_payload = json.dumps(metrics, default=str, separators=(",", ":"))
        await self._mqtt.publish(state_topic, state_payload, qos=qos, retain=retain)

        self._publish_count += 1
        self._last_publish_time = time.monotonic()
        logger.debug(
            "Published metrics to %d topics (cycle #%d)",
            len(_METRIC_FIELDS) + 1,
            self._publish_count,
        )

    # -- background loop ----------------------------------------------------

    async def _publish_loop(self) -> None:
        """Periodically publish cached metrics to MQTT.

        Runs until :meth:`stop` is called.  Errors are caught and counted
        so the loop never dies unexpectedly.
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._publish_interval,
                )
                # stop_event was set — exit
                return
            except asyncio.TimeoutError:
                pass

            if self._latest_metrics is None:
                logger.debug("No metrics cached yet — skipping publish cycle")
                continue

            if not self._mqtt.is_connected:
                logger.debug("MQTT not connected — skipping publish cycle")
                continue

            try:
                await self.publish_metrics(self._latest_metrics)
            except Exception as exc:
                self._error_count += 1
                logger.warning(
                    "Metric publish failed (error #%d): %s",
                    self._error_count,
                    exc,
                )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _serialize_value(value: Any) -> str:
        """Serialize a metric value to a string suitable for MQTT payload.

        Lists and dicts are JSON-encoded.  Scalars are converted with ``str()``.
        Floats are rounded to 1 decimal place for readability.
        """
        if isinstance(value, list | dict):
            return json.dumps(value, default=str, separators=(",", ":"))
        if isinstance(value, float):
            return str(round(value, 1))
        return str(value)

    def get_status(self) -> dict[str, Any]:
        """Return diagnostic information about the publisher.

        Returns
        -------
        dict
            Keys: ``running``, ``publish_count``, ``error_count``,
            ``publish_interval``, ``topic_prefix``, ``has_cached_metrics``.
        """
        return {
            "running": self.is_running,
            "publish_count": self._publish_count,
            "error_count": self._error_count,
            "publish_interval": self._publish_interval,
            "topic_prefix": self._topic_prefix,
            "has_cached_metrics": self._latest_metrics is not None,
        }
