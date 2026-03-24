"""OLED display plugin implementation.

Implements the CasePlugin protocol for driving the SSD1306 128x64 OLED
display.  Cycles through configurable screens showing clock, system metrics,
temperatures, and fan duty information.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

try:
    from PIL import Image, ImageDraw
    _pil_available = True
except ImportError:
    _pil_available = False

from casectl.config.models import OledConfig, OledScreenConfig
from casectl.plugins.base import PluginContext, PluginStatus

logger = logging.getLogger(__name__)

# Display dimensions.
DISPLAY_WIDTH: int = 128
DISPLAY_HEIGHT: int = 64

# Screen names in order.
SCREEN_NAMES: list[str] = ["clock", "metrics", "temperature", "fan_duty"]


class OledDisplayPlugin:
    """OLED display plugin that cycles through information screens.

    Built-in screens:
    - **clock**: Current date and time.
    - **metrics**: CPU%, MEM%, DISK% with text-based bars.
    - **temperature**: CPU and case temperatures.
    - **fan_duty**: Three-channel fan duty percentages.

    The plugin uses PIL to render 128x64 monochrome images and sends
    them to the SSD1306 OLED via the luma.oled driver.
    """

    name: str = "oled-display"
    version: str = "0.1.0"
    description: str = "OLED display with rotating clock, metrics, temperature, and fan screens"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._task: asyncio.Task[None] | None = None
        self._degraded: bool = False
        self._current_screen: int = 0

        # Latest metrics snapshot from the event bus.
        self._latest_metrics: dict[str, Any] = {}

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes and subscribe to metrics events.

        Parameters
        ----------
        ctx:
            The plugin context provided by the plugin host.
        """
        self._ctx = ctx

        # Register routes.
        from casectl.plugins.oled import routes

        routes.configure(
            get_status=self._get_oled_status,
            get_config=lambda: ctx._config_manager,
        )
        ctx.register_routes(routes.router)

        # Subscribe to metrics updates for rendering screen content.
        ctx.on_event("metrics_updated", self._on_metrics_updated)

        ctx.logger.info("OLED display plugin setup complete")

    async def start(self) -> None:
        """Launch the screen cycling background task."""
        self._task = asyncio.create_task(
            self._display_loop(),
            name="oled-display-loop",
        )
        logger.info("OLED display background task started")

    async def stop(self) -> None:
        """Cancel the display task and clear the screen."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("OLED display background task stopped")

        # Clear the display on shutdown.
        self._clear_display()
        self._task = None

    def get_status(self) -> dict[str, Any]:
        """Return the current OLED display status."""
        if self._degraded:
            status = PluginStatus.DEGRADED
        elif self._task is not None and not self._task.done():
            status = PluginStatus.HEALTHY
        else:
            status = PluginStatus.STOPPED

        return {
            "status": status,
            "current_screen": self._current_screen,
            "screen_names": list(SCREEN_NAMES),
            "degraded": self._degraded,
        }

    # ------------------------------------------------------------------
    # Internal status accessor for routes
    # ------------------------------------------------------------------

    def _get_oled_status(self) -> dict[str, Any]:
        """Return status dict for the routes module, including config state."""
        config = self._get_cached_config()
        screens_enabled = [
            config.screens[i].enabled if i < len(config.screens) else True
            for i in range(len(SCREEN_NAMES))
        ]

        return {
            "current_screen": self._current_screen,
            "screen_names": list(SCREEN_NAMES),
            "screens_enabled": screens_enabled,
            "rotation": config.rotation,
            "degraded": self._degraded,
        }

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_metrics_updated(self, data: Any) -> None:
        """Cache metrics snapshot for screen rendering."""
        if isinstance(data, dict):
            self._latest_metrics = data

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _get_cached_config(self) -> OledConfig:
        """Return OledConfig from the last known config, or defaults."""
        if self._ctx is None or self._ctx._config_manager is None:
            return OledConfig()
        try:
            cfg = self._ctx._config_manager.config
            return cfg.oled
        except (RuntimeError, AttributeError):
            return OledConfig()

    async def _get_oled_config(self) -> OledConfig:
        """Read the current OLED configuration from the config manager."""
        if self._ctx is None or self._ctx._config_manager is None:
            return OledConfig()
        try:
            raw = await self._ctx._config_manager.get("oled")
            return OledConfig.model_validate(raw)
        except Exception:
            logger.debug("Failed to read OLED config — using defaults", exc_info=True)
            return OledConfig()

    # ------------------------------------------------------------------
    # Display operations
    # ------------------------------------------------------------------

    def _get_oled_device(self):
        """Return the OLED device driver, or None."""
        if self._ctx is None:
            return None
        return self._ctx.get_hardware().oled

    def _clear_display(self) -> None:
        """Clear the OLED display."""
        oled = self._get_oled_device()
        if oled is not None and oled.available:
            try:
                oled.clear()
            except Exception:
                logger.debug("Failed to clear OLED display", exc_info=True)

    async def _render_to_display(self, image: Image.Image) -> None:
        """Send a rendered image to the OLED display."""
        oled = self._get_oled_device()
        if oled is None or not oled.available:
            self._degraded = oled is None
            return

        try:
            await oled.async_render_image(image)
            self._degraded = False
        except Exception:
            logger.warning("Failed to render image to OLED", exc_info=True)
            self._degraded = True

    # ------------------------------------------------------------------
    # Screen renderers
    # ------------------------------------------------------------------

    def _render_clock(self) -> Image.Image:
        """Render the clock screen: date and time in large text.

        Returns a 128x64 monochrome PIL Image.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        weekday_str = now.strftime("%A")
        time_str = now.strftime("%H:%M:%S")

        # Layout: weekday at top, date in middle, time at bottom.
        draw.text((2, 2), weekday_str, fill=1)
        draw.text((2, 20), date_str, fill=1)
        draw.text((2, 40), time_str, fill=1)

        return image

    def _render_metrics(self) -> Image.Image:
        """Render the metrics screen: CPU%, MEM%, DISK% with text bars.

        Returns a 128x64 monochrome PIL Image.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        cpu = self._latest_metrics.get("cpu_percent", 0.0)
        mem = self._latest_metrics.get("memory_percent", 0.0)
        disk = self._latest_metrics.get("disk_percent", 0.0)

        draw.text((2, 2), "System Metrics", fill=1)

        # Draw text bars for each metric.
        y_offset = 18
        for label, value in [("CPU", cpu), ("MEM", mem), ("DSK", disk)]:
            pct = max(0.0, min(100.0, value))
            bar_width = int(pct / 100.0 * 80)
            text = f"{label} {pct:4.1f}%"
            draw.text((2, y_offset), text, fill=1)

            # Draw bar outline and fill.
            bar_x = 46
            bar_y = y_offset + 1
            bar_h = 8
            draw.rectangle([bar_x, bar_y, bar_x + 80, bar_y + bar_h], outline=1)
            if bar_width > 0:
                draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_h], fill=1)

            y_offset += 15

        return image

    def _render_temperature(self) -> Image.Image:
        """Render the temperature screen: CPU temp and case temp.

        Returns a 128x64 monochrome PIL Image.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        cpu_temp = self._latest_metrics.get("cpu_temp", 0.0)
        case_temp = self._latest_metrics.get("case_temp", 0.0)

        draw.text((2, 2), "Temperature", fill=1)
        draw.text((2, 22), f"CPU:  {cpu_temp:5.1f} C", fill=1)
        draw.text((2, 42), f"Case: {case_temp:5.1f} C", fill=1)

        return image

    def _render_fan_duty(self) -> Image.Image:
        """Render the fan duty screen: 3 channel duty percentages.

        Reads duty from cached metrics (hardware range 0-255) and displays
        as percentages (0-100%).

        Returns a 128x64 monochrome PIL Image.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        fan_duty = self._latest_metrics.get("fan_duty", [0, 0, 0])
        if not isinstance(fan_duty, list) or len(fan_duty) < 3:
            fan_duty = [0, 0, 0]

        draw.text((2, 2), "Fan Duty", fill=1)

        y_offset = 20
        for i in range(3):
            duty_hw = fan_duty[i] if i < len(fan_duty) else 0
            duty_pct = duty_hw / 255.0 * 100.0
            text = f"Fan {i + 1}: {duty_pct:5.1f}%"
            draw.text((2, y_offset), text, fill=1)
            y_offset += 15

        return image

    # Screen renderer dispatch table.
    _RENDERERS = {
        0: _render_clock,
        1: _render_metrics,
        2: _render_temperature,
        3: _render_fan_duty,
    }

    # ------------------------------------------------------------------
    # Screen cycling logic
    # ------------------------------------------------------------------

    def _find_next_enabled_screen(self, config: OledConfig) -> int | None:
        """Find the next enabled screen index after the current one.

        Returns None if no screens are enabled.
        """
        num_screens = len(SCREEN_NAMES)
        for offset in range(1, num_screens + 1):
            idx = (self._current_screen + offset) % num_screens
            if idx < len(config.screens) and config.screens[idx].enabled:
                return idx
        return None

    def _get_screen_display_time(self, config: OledConfig, screen_idx: int) -> float:
        """Return the display time for the given screen index."""
        if screen_idx < len(config.screens):
            return config.screens[screen_idx].display_time
        return 5.0  # default

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _display_loop(self) -> None:
        """Cycle through enabled screens, rendering each for its display_time.

        Reads configuration each cycle to pick up enable/disable changes.
        Catches all exceptions to prevent the task from dying.
        """
        logger.info("OLED display loop started")

        while True:
            try:
                config = await self._get_oled_config()

                # Find an enabled screen to display.
                num_screens = len(SCREEN_NAMES)
                # If current screen is disabled, advance to next enabled one.
                current_enabled = (
                    self._current_screen < len(config.screens)
                    and config.screens[self._current_screen].enabled
                )

                if not current_enabled:
                    next_screen = self._find_next_enabled_screen(config)
                    if next_screen is None:
                        # No screens enabled — sleep and retry.
                        await asyncio.sleep(1.0)
                        continue
                    self._current_screen = next_screen

                # Render the current screen.
                renderer = self._RENDERERS.get(self._current_screen)
                if renderer is not None:
                    image = renderer(self)
                    await self._render_to_display(image)

                # Wait for the configured display time.
                display_time = self._get_screen_display_time(config, self._current_screen)
                await asyncio.sleep(display_time)

                # Advance to the next enabled screen.
                next_screen = self._find_next_enabled_screen(config)
                if next_screen is not None:
                    self._current_screen = next_screen

            except asyncio.CancelledError:
                logger.info("OLED display loop cancelled")
                raise
            except Exception:
                logger.error("OLED display loop error", exc_info=True)
                self._degraded = True
                await asyncio.sleep(5.0)
