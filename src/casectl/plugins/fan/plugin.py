"""Fan control plugin implementation.

Implements the CasePlugin protocol for managing three STM32-driven case fans
with configurable modes: follow_temp, follow_rpi, manual, and off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from casectl.plugins.base import PluginContext, PluginStatus
from casectl.plugins.fan.controller import FanController

logger = logging.getLogger(__name__)


class FanControlPlugin:
    """Fan control plugin that manages case fan duty cycles.

    The plugin launches a background task that polls every 3 seconds, reads
    the fan configuration, computes the appropriate duty cycle for the
    configured mode, and writes the result to the STM32 expansion board.

    The STM32 is always set to FanHwMode.MANUAL — casectl controls duty
    values directly, regardless of the config-layer FanMode.
    """

    name: str = "fan-control"
    version: str = "0.1.0"
    description: str = "Case fan speed control with temperature tracking and multiple modes"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._controller: FanController | None = None
        self._task: asyncio.Task[None] | None = None

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes, subscribe to metrics events, and create the controller.

        Parameters
        ----------
        ctx:
            The plugin context provided by the plugin host.
        """
        self._ctx = ctx
        hw = ctx.get_hardware()

        # Create the fan controller with access to config, expansion board, and system info.
        self._controller = FanController(
            config_manager=ctx._config_manager,
            expansion=hw.expansion,
            system_info=hw.system_info,
        )

        # Register routes — dependencies are provided via app.state so that
        # route handlers use FastAPI Depends() instead of module-level globals.
        from casectl.plugins.fan import routes

        ctx.set_app_state("fan_controller", self._controller)
        ctx.set_app_state("fan_config_manager", ctx._config_manager)
        ctx.register_routes(routes.router)

        # Subscribe to metrics_updated events so the controller can use
        # cached temperature readings instead of extra I2C calls.
        ctx.on_event("metrics_updated", self._on_metrics_updated)

        ctx.logger.info("Fan control plugin setup complete")

    async def start(self) -> None:
        """Launch the fan control background task."""
        if self._controller is None:
            logger.error("Cannot start fan control — controller not initialised")
            return

        self._task = asyncio.create_task(
            self._controller.run(),
            name="fan-control-loop",
        )
        logger.info("Fan control background task started")

    async def stop(self) -> None:
        """Cancel the fan control background task and clean up."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("Fan control background task stopped")

        self._task = None

    def get_status(self) -> dict[str, Any]:
        """Return the current fan control status.

        Returns
        -------
        dict
            Contains ``status``, ``mode``, ``duty``, and ``degraded`` keys.
        """
        if self._controller is None:
            return {
                "status": PluginStatus.STOPPED,
                "mode": "unknown",
                "duty": [0, 0, 0],
                "degraded": False,
            }

        if self._controller.degraded:
            status = PluginStatus.DEGRADED
        elif self._task is not None and not self._task.done():
            status = PluginStatus.HEALTHY
        else:
            status = PluginStatus.STOPPED

        return {
            "status": status,
            "mode": self._controller.current_mode.name.lower(),
            "duty": self._controller.current_duty,
            "degraded": self._controller.degraded,
        }

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_metrics_updated(self, data: Any) -> None:
        """Handle metrics_updated events from the system monitor plugin.

        Passes the metrics snapshot to the controller so it can read CPU
        temperature without additional sysfs or I2C calls.
        """
        if self._controller is not None and isinstance(data, dict):
            self._controller.update_metrics(data)
