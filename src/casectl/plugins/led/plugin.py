"""LED control plugin implementation.

Implements the CasePlugin protocol for managing the RGB LEDs on the STM32
expansion board.  Supports rainbow, breathing, follow-temperature, manual
colour, and off modes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from casectl.config.models import LedConfig, LedMode
from casectl.hardware.expansion import LedHwMode
from casectl.plugins.base import PluginContext, PluginStatus

logger = logging.getLogger(__name__)

# Polling interval for the LED control loop.
POLL_INTERVAL: float = 3.0

# Mapping from config-layer LedMode to the action the plugin should take.
# Modes that delegate to the STM32 only need a hardware mode write.
# MANUAL and OFF require additional commands (set colour or close).
_MODE_TO_HW: dict[LedMode, LedHwMode] = {
    LedMode.RAINBOW: LedHwMode.RAINBOW,
    LedMode.BREATHING: LedHwMode.BREATHING,
    LedMode.FOLLOW_TEMP: LedHwMode.FOLLOWING,
    LedMode.MANUAL: LedHwMode.RGB,
    LedMode.OFF: LedHwMode.CLOSE,
}


class LedControlPlugin:
    """LED control plugin that manages RGB LEDs on the expansion board.

    The plugin runs a background task that periodically reads the LED config
    and applies the appropriate hardware mode and colour settings.

    Mode mapping:
    - RAINBOW / BREATHING: Delegate entirely to STM32 firmware effects.
    - MANUAL: Set STM32 to RGB mode, then write the configured colour.
    - OFF: Set STM32 to CLOSE mode.
    - FOLLOW_TEMP: Set STM32 to FOLLOWING mode (firmware handles colour).
    - CUSTOM: Treated as MANUAL fallback.
    """

    name: str = "led-control"
    version: str = "0.1.0"
    description: str = "RGB LED control with rainbow, breathing, temperature-follow, and manual modes"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._task: asyncio.Task[None] | None = None
        self._degraded: bool = False
        self._current_mode: LedMode = LedMode.OFF
        self._current_color: dict[str, int] = {"red": 0, "green": 0, "blue": 0}
        # Track what we last wrote to avoid redundant I2C writes.
        self._last_applied_mode: LedMode | None = None
        self._last_applied_color: tuple[int, int, int] | None = None

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes and store the plugin context.

        Parameters
        ----------
        ctx:
            The plugin context provided by the plugin host.
        """
        self._ctx = ctx

        # Register routes.
        from casectl.plugins.led import routes

        routes.configure(
            get_status=self._get_led_status,
            get_config=lambda: ctx._config_manager,
        )
        ctx.register_routes(routes.router)

        ctx.logger.info("LED control plugin setup complete")

    async def start(self) -> None:
        """Launch the LED control background task."""
        self._task = asyncio.create_task(
            self._control_loop(),
            name="led-control-loop",
        )
        logger.info("LED control background task started")

    async def stop(self) -> None:
        """Cancel the LED control background task and turn off LEDs."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("LED control background task stopped")

        # Turn off LEDs on shutdown.
        await self._apply_off()
        self._task = None

    def get_status(self) -> dict[str, Any]:
        """Return the current LED control status."""
        if self._degraded:
            status = PluginStatus.DEGRADED
        elif self._task is not None and not self._task.done():
            status = PluginStatus.HEALTHY
        else:
            status = PluginStatus.STOPPED

        return {
            "status": status,
            "mode": self._current_mode.name.lower(),
            "color": dict(self._current_color),
            "degraded": self._degraded,
        }

    # ------------------------------------------------------------------
    # Internal status accessor for routes
    # ------------------------------------------------------------------

    def _get_led_status(self) -> dict[str, Any]:
        """Return status dict for the routes module."""
        return {
            "mode": self._current_mode.name.lower(),
            "color": dict(self._current_color),
            "degraded": self._degraded,
        }

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def _get_led_config(self) -> LedConfig:
        """Read the current LED configuration from the config manager."""
        if self._ctx is None or self._ctx._config_manager is None:
            return LedConfig()
        try:
            raw = await self._ctx._config_manager.get("led")
            return LedConfig.model_validate(raw)
        except Exception:
            logger.debug("Failed to read LED config — using defaults", exc_info=True)
            return LedConfig()

    # ------------------------------------------------------------------
    # Hardware operations
    # ------------------------------------------------------------------

    def _get_expansion(self):
        """Return the expansion board driver, or None."""
        if self._ctx is None:
            return None
        return self._ctx.get_hardware().expansion

    async def _apply_mode(self, config: LedConfig) -> None:
        """Apply the configured LED mode and colour to the STM32.

        Only writes to hardware when the mode or colour has actually changed,
        to reduce unnecessary I2C traffic.
        """
        mode = config.mode
        self._current_mode = mode
        color = (config.red_value, config.green_value, config.blue_value)
        self._current_color = {"red": color[0], "green": color[1], "blue": color[2]}

        expansion = self._get_expansion()
        if expansion is None or not expansion.connected:
            if expansion is not None and not expansion.connected:
                self._degraded = True
            return

        try:
            if mode == LedMode.OFF:
                if self._last_applied_mode != mode:
                    await expansion.async_set_led_mode(LedHwMode.CLOSE)
                    self._last_applied_mode = mode
                    self._last_applied_color = None

            elif mode == LedMode.MANUAL:
                # Set RGB mode then write colour.
                if self._last_applied_mode != mode:
                    await expansion.async_set_led_mode(LedHwMode.RGB)
                    self._last_applied_mode = mode

                if self._last_applied_color != color:
                    await expansion.async_set_all_led_color(color[0], color[1], color[2])
                    self._last_applied_color = color

            elif mode == LedMode.RAINBOW:
                if self._last_applied_mode != mode:
                    await expansion.async_set_led_mode(LedHwMode.RAINBOW)
                    self._last_applied_mode = mode
                    self._last_applied_color = None

            elif mode == LedMode.BREATHING:
                if self._last_applied_mode != mode:
                    await expansion.async_set_led_mode(LedHwMode.BREATHING)
                    self._last_applied_mode = mode
                    self._last_applied_color = None

            elif mode == LedMode.FOLLOW_TEMP:
                if self._last_applied_mode != mode:
                    await expansion.async_set_led_mode(LedHwMode.FOLLOWING)
                    self._last_applied_mode = mode
                    self._last_applied_color = None

            elif mode == LedMode.CUSTOM:
                # Treat CUSTOM as MANUAL fallback.
                if self._last_applied_mode != mode:
                    await expansion.async_set_led_mode(LedHwMode.RGB)
                    self._last_applied_mode = mode

                if self._last_applied_color != color:
                    await expansion.async_set_all_led_color(color[0], color[1], color[2])
                    self._last_applied_color = color

            self._degraded = expansion.degraded

        except OSError:
            logger.warning("Failed to apply LED mode to hardware", exc_info=True)
            self._degraded = True

    async def _apply_off(self) -> None:
        """Turn off LEDs — used during shutdown."""
        expansion = self._get_expansion()
        if expansion is None or not expansion.connected:
            return

        try:
            await expansion.async_set_led_mode(LedHwMode.CLOSE)
        except OSError:
            logger.debug("Failed to turn off LEDs during shutdown", exc_info=True)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _control_loop(self) -> None:
        """Run the LED control loop indefinitely.

        Polls the config every POLL_INTERVAL seconds and applies changes
        to the hardware.
        """
        logger.info("LED control loop started (poll interval: %.1fs)", POLL_INTERVAL)

        while True:
            try:
                config = await self._get_led_config()
                await self._apply_mode(config)
            except asyncio.CancelledError:
                logger.info("LED control loop cancelled")
                raise
            except Exception:
                logger.error("LED control loop error", exc_info=True)
                self._degraded = True

            await asyncio.sleep(POLL_INTERVAL)
