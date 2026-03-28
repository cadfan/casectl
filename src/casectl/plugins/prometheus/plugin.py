"""Prometheus metrics exporter plugin implementation.

Implements the CasePlugin protocol for exposing system metrics in Prometheus
text exposition format.  Subscribes to ``metrics_updated`` events from the
system monitor plugin and caches the latest snapshot for scraping.
"""

from __future__ import annotations

import logging
from typing import Any

from casectl.plugins.base import PluginContext, PluginStatus

logger = logging.getLogger(__name__)


class PrometheusPlugin:
    """Prometheus metrics exporter plugin.

    This plugin subscribes to ``metrics_updated`` events emitted by the
    system monitor plugin and caches the latest metrics snapshot.  The
    cached data is served in Prometheus text exposition format via
    ``GET /metrics``.

    No background task is needed — the plugin is purely event-driven
    and serves cached data on demand.

    Metric names follow Prometheus naming conventions:
    - ``casectl_cpu_temp_celsius`` — CPU die temperature
    - ``casectl_case_temp_celsius`` — Case / ambient temperature
    - ``casectl_cpu_usage_ratio`` — CPU utilisation (0-1)
    - ``casectl_memory_usage_ratio`` — Memory utilisation (0-1)
    - ``casectl_disk_usage_ratio`` — Root disk utilisation (0-1)
    - ``casectl_fan_duty_ratio{channel="0|1|2"}`` — Fan duty cycle (0-1)
    - ``casectl_fan_rpm{channel="0|1|2"}`` — Fan RPM
    """

    name: str = "prometheus"
    version: str = "0.1.0"
    description: str = "Prometheus metrics exporter in text exposition format"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._latest_metrics: dict[str, Any] | None = None

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes and subscribe to metrics events.

        Parameters
        ----------
        ctx:
            The plugin context provided by the plugin host.
        """
        self._ctx = ctx

        # Register routes — dependencies are provided via app.state so that
        # route handlers use FastAPI Depends() instead of module-level globals.
        from casectl.plugins.prometheus import routes

        ctx.set_app_state("prometheus_get_metrics", lambda: self._latest_metrics)
        ctx.register_routes(routes.router)

        # Subscribe to metrics_updated events from the system monitor.
        ctx.on_event("metrics_updated", self._on_metrics_updated)

        ctx.logger.info("Prometheus plugin setup complete")

    async def start(self) -> None:
        """No background task needed — plugin is event-driven."""
        logger.info("Prometheus plugin started (event-driven, no background task)")

    async def stop(self) -> None:
        """Clean up cached metrics."""
        self._latest_metrics = None
        logger.info("Prometheus plugin stopped")

    def get_status(self) -> dict[str, Any]:
        """Return the current plugin status."""
        has_data = self._latest_metrics is not None
        status = PluginStatus.HEALTHY

        return {
            "status": status,
            "has_metrics": has_data,
        }

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_metrics_updated(self, data: Any) -> None:
        """Cache the latest metrics snapshot from the system monitor.

        Parameters
        ----------
        data:
            A dict matching the SystemMetrics schema, emitted by the
            system-monitor plugin.
        """
        if isinstance(data, dict):
            self._latest_metrics = data
