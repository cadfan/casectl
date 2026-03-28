"""System monitor plugin implementation.

Implements the CasePlugin protocol for collecting system metrics (CPU, memory,
disk, temperature, fan duty, motor speed) every 2 seconds and emitting them
on the event bus for other plugins to consume.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from casectl.config.models import SystemMetrics
from casectl.plugins.base import PluginContext, PluginStatus

logger = logging.getLogger(__name__)

# Collection interval in seconds.
COLLECT_INTERVAL: float = 2.0


class SystemMonitorPlugin:
    """System monitor plugin that collects and broadcasts metrics.

    The plugin runs a background task that collects system metrics every
    2 seconds using :class:`SystemInfo.get_all_metrics` and reads additional
    data from the STM32 expansion board (case temperature, fan duty, motor
    speeds).

    The collected :class:`SystemMetrics` is stored in memory for API access
    and emitted on the ``metrics_updated`` event for other plugins (fan
    control, OLED display, Prometheus) to consume.
    """

    name: str = "system-monitor"
    version: str = "0.1.0"
    description: str = "System metrics collection with event bus broadcasting"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._task: asyncio.Task[None] | None = None
        self._degraded: bool = False
        self._latest_metrics: dict[str, Any] | None = None

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes and store the plugin context.

        Parameters
        ----------
        ctx:
            The plugin context provided by the plugin host.
        """
        self._ctx = ctx

        # Register routes — dependencies are provided via app.state so that
        # route handlers use FastAPI Depends() instead of module-level globals.
        from casectl.plugins.monitor import routes

        ctx.set_app_state("monitor_get_metrics", lambda: self._latest_metrics)
        ctx.register_routes(routes.router)

        ctx.logger.info("System monitor plugin setup complete")

    async def start(self) -> None:
        """Launch the metrics collection background task."""
        self._task = asyncio.create_task(
            self._collect_loop(),
            name="system-monitor-loop",
        )
        logger.info("System monitor background task started")

    async def stop(self) -> None:
        """Cancel the metrics collection background task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("System monitor background task stopped")

        self._task = None

    def get_status(self) -> dict[str, Any]:
        """Return the current monitor status including latest metrics."""
        if self._degraded:
            status = PluginStatus.DEGRADED
        elif self._task is not None and not self._task.done():
            status = PluginStatus.HEALTHY
        else:
            status = PluginStatus.STOPPED

        result: dict[str, Any] = {
            "status": status,
            "degraded": self._degraded,
            "has_metrics": self._latest_metrics is not None,
        }

        if self._latest_metrics is not None:
            result["metrics"] = self._latest_metrics

        return result

    # ------------------------------------------------------------------
    # Metrics collection
    # ------------------------------------------------------------------

    async def _collect_metrics(self) -> dict[str, Any]:
        """Collect all system metrics from SystemInfo and the expansion board.

        Returns a dict matching the SystemMetrics schema.
        """
        hw = self._ctx.get_hardware() if self._ctx is not None else None
        system_info = hw.system_info if hw is not None else None
        expansion = hw.expansion if hw is not None else None

        # -- System metrics from psutil / sysfs --------------------------------
        cpu_percent: float = 0.0
        memory_percent: float = 0.0
        disk_percent: float = 0.0
        swap_percent: float = 0.0
        swap_used_gb: float = 0.0
        swap_total_gb: float = 0.0
        cpu_temp: float = 0.0
        ip_address: str = ""
        rpi_fan_duty: int = 0
        date_str: str = ""
        weekday_str: str = ""
        time_str: str = ""

        if system_info is not None:
            try:
                all_metrics = await asyncio.to_thread(system_info.get_all_metrics)
                cpu_percent = all_metrics.cpu_usage
                cpu_temp = all_metrics.cpu_temperature
                memory_percent = all_metrics.memory.percent
                disk_percent = all_metrics.disk.percent
                swap_percent = all_metrics.swap.percent
                swap_used_gb = all_metrics.swap.used_gb
                swap_total_gb = all_metrics.swap.total_gb
                ip_address = all_metrics.ip_address
                rpi_fan_duty = all_metrics.fan_duty
                date_str = all_metrics.date
                weekday_str = all_metrics.weekday
                time_str = all_metrics.time
            except Exception:
                logger.debug("Failed to collect system metrics", exc_info=True)

        # -- Expansion board readings ------------------------------------------
        case_temp: float = 0.0
        fan_duty: list[int] = [0, 0, 0]
        motor_speed: list[int] = [0, 0, 0]

        if expansion is not None and expansion.connected:
            try:
                case_temp = float(await expansion.async_get_temperature())
            except OSError:
                logger.debug("Failed to read case temperature", exc_info=True)

            try:
                duty = await expansion.async_get_fan_duty()
                fan_duty = list(duty)
            except OSError:
                logger.debug("Failed to read fan duty", exc_info=True)

            try:
                speeds = await expansion.async_get_motor_speed()
                motor_speed = list(speeds)
            except OSError:
                logger.debug("Failed to read motor speeds", exc_info=True)

            self._degraded = expansion.degraded
        elif expansion is not None:
            self._degraded = True

        # Build the metrics dict.
        metrics = SystemMetrics(
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            disk_percent=disk_percent,
            cpu_temp=cpu_temp,
            case_temp=case_temp,
            ip_address=ip_address,
            fan_duty=fan_duty,
            motor_speed=motor_speed,
            date=date_str,
            weekday=weekday_str,
            time=time_str,
            rpi_fan_duty=rpi_fan_duty,
            swap_percent=swap_percent,
            swap_used_gb=swap_used_gb,
            swap_total_gb=swap_total_gb,
        )

        return metrics.model_dump()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _collect_loop(self) -> None:
        """Collect metrics every COLLECT_INTERVAL seconds and emit events.

        Catches all exceptions to prevent the task from dying.
        """
        logger.info(
            "System monitor collection loop started (interval: %.1fs)",
            COLLECT_INTERVAL,
        )

        while True:
            try:
                metrics = await self._collect_metrics()
                self._latest_metrics = metrics

                # Emit the metrics_updated event for other plugins.
                if self._ctx is not None:
                    self._ctx.emit_event("metrics_updated", metrics)

            except asyncio.CancelledError:
                logger.info("System monitor collection loop cancelled")
                raise
            except Exception:
                logger.error("System monitor collection error", exc_info=True)
                self._degraded = True

            await asyncio.sleep(COLLECT_INTERVAL)
