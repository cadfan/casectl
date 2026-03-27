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
    from PIL import Image, ImageDraw, ImageFont
    _pil_available = True
except ImportError:
    _pil_available = False

from casectl.config.models import OledConfig, OledScreenConfig
from casectl.plugins.base import PluginContext, PluginStatus

logger = logging.getLogger(__name__)

# Display dimensions.
DISPLAY_WIDTH: int = 128
DISPLAY_HEIGHT: int = 64

# ---------------------------------------------------------------------------
# Fonts — loaded once at import, with graceful fallback to default bitmap.
# ---------------------------------------------------------------------------
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
_FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a TrueType font, falling back to PIL default on failure."""
    if not _pil_available:
        return ImageFont.load_default()  # type: ignore[return-value]
    try:
        return ImageFont.truetype(path, size)
    except (OSError, IOError):
        return ImageFont.load_default()


# Pre-load fonts at common sizes used by the screen renderers.
_FONT_LARGE: ImageFont.FreeTypeFont | ImageFont.ImageFont = _load_font(_FONT_PATH, 24)
_FONT_MED: ImageFont.FreeTypeFont | ImageFont.ImageFont = _load_font(_FONT_PATH, 14)
_FONT_SMALL: ImageFont.FreeTypeFont | ImageFont.ImageFont = _load_font(_FONT_PATH, 11)
_FONT_TINY: ImageFont.FreeTypeFont | ImageFont.ImageFont = _load_font(_FONT_PATH_REGULAR, 9)

import math as _math


def _draw_arc_gauge(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    radius: int,
    pct: float,
    *,
    ticks: int = 10,
    start_angle: float = 225.0,
    end_angle: float = 315.0,
) -> None:
    """Draw a sweep-arc gauge with tick marks and a needle.

    *pct* is in the range 0-100. The arc sweeps clockwise from
    *start_angle* to *end_angle* (degrees, 0 = right, counter-clockwise).
    """
    sweep = (360.0 - start_angle + end_angle) % 360.0
    if sweep == 0:
        sweep = 360.0

    # Arc outline.
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    draw.arc(bbox, start=start_angle, end=end_angle, fill=1, width=1)

    # Tick marks.
    for i in range(ticks + 1):
        frac = i / ticks
        angle_deg = start_angle + frac * sweep
        angle_rad = _math.radians(angle_deg)
        inner = radius - 3
        outer = radius
        x0 = cx + inner * _math.cos(angle_rad)
        y0 = cy + inner * _math.sin(angle_rad)
        x1 = cx + outer * _math.cos(angle_rad)
        y1 = cy + outer * _math.sin(angle_rad)
        draw.line([(x0, y0), (x1, y1)], fill=1)

    # Needle.
    clamped = max(0.0, min(100.0, pct))
    needle_angle_deg = start_angle + (clamped / 100.0) * sweep
    needle_rad = _math.radians(needle_angle_deg)
    nx = cx + (radius - 5) * _math.cos(needle_rad)
    ny = cy + (radius - 5) * _math.sin(needle_rad)
    draw.line([(cx, cy), (nx, ny)], fill=1, width=1)
    draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=1)


def _draw_filled_circle_pct(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    radius: int,
    pct: float,
) -> None:
    """Draw a circular percentage gauge with the value shown below."""
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    # Background circle outline.
    draw.ellipse(bbox, outline=1)
    # Filled arc showing percentage (PIL arc goes clockwise from 3-o'clock).
    if pct > 0:
        extent = max(1, pct / 100.0 * 360.0)
        draw.pieslice(bbox, start=-90, end=-90 + extent, fill=1)
    # Percentage text below the circle — always white on black, always readable.
    text = f"{int(pct)}%"
    tw = draw.textlength(text, font=_FONT_TINY)
    tx = cx - tw / 2
    ty = cy + radius + 2
    draw.text((tx, ty), text, fill=1, font=_FONT_TINY)


def _center_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    *,
    region_width: int = DISPLAY_WIDTH,
    x_offset: int = 0,
) -> None:
    """Draw *text* horizontally centered within *region_width* at the given *y*."""
    tw = draw.textlength(text, font=font)
    x = x_offset + (region_width - tw) / 2
    draw.text((x, y), text, fill=1, font=font)


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
        """Render the clock screen with bordered rows and large time.

        Layout matches the Freenove original: border box with two
        horizontal dividers creating three rows — date, large time,
        weekday.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        weekday_str = now.strftime("%A")
        time_str = now.strftime("%H:%M:%S")

        # Outer border and row dividers.
        draw.rectangle([0, 0, 127, 63], outline=1)
        draw.line([(0, 16), (127, 16)], fill=1)
        draw.line([(0, 48), (127, 48)], fill=1)

        # Row 1: date (centered, medium font).
        _center_text(draw, date_str, 2, _FONT_SMALL)
        # Row 2: time (centered, large font) — the hero element.
        _center_text(draw, time_str, 20, _FONT_LARGE)
        # Row 3: weekday (centered, medium font).
        _center_text(draw, weekday_str, 50, _FONT_SMALL)

        return image

    def _render_metrics(self) -> Image.Image:
        """Render the metrics screen with IP header and circle gauges.

        Layout: bordered box, IP address row at top, then three columns
        each with a label and a filled-circle percentage gauge for CPU,
        MEM, and DISK.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        cpu = self._latest_metrics.get("cpu_percent", 0.0)
        mem = self._latest_metrics.get("memory_percent", 0.0)
        disk = self._latest_metrics.get("disk_percent", 0.0)
        ip = self._latest_metrics.get("ip_address", "")

        # Outer border and header divider.
        draw.rectangle([0, 0, 127, 63], outline=1)
        draw.line([(0, 16), (127, 16)], fill=1)

        # Column dividers below header.
        draw.line([(43, 16), (43, 63)], fill=1)
        draw.line([(86, 16), (86, 63)], fill=1)

        # Header row: IP address.
        ip_display = f"IP:{ip}" if ip else "IP: —"
        _center_text(draw, ip_display, 3, _FONT_SMALL)

        # Column labels.
        _center_text(draw, "CPU", 18, _FONT_TINY, region_width=43, x_offset=0)
        _center_text(draw, "MEM", 18, _FONT_TINY, region_width=43, x_offset=43)
        _center_text(draw, "DISK", 18, _FONT_TINY, region_width=42, x_offset=86)

        # Circle gauges — raised to leave room for percentage text below.
        _draw_filled_circle_pct(draw, 21, 38, 12, cpu)
        _draw_filled_circle_pct(draw, 64, 38, 12, mem)
        _draw_filled_circle_pct(draw, 107, 38, 12, disk)

        return image

    def _render_temperature(self) -> Image.Image:
        """Render the temperature screen with analogue dial gauges.

        Two-column layout: Pi (CPU) temp on the left, Case temp on the
        right.  Each column has a label, a sweep-arc dial gauge, and
        the numeric reading below.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        cpu_temp = self._latest_metrics.get("cpu_temp", 0.0)
        case_temp = self._latest_metrics.get("case_temp", 0.0)

        # Border and vertical divider.
        draw.rectangle([0, 0, 127, 63], outline=1)
        draw.line([(64, 0), (64, 63)], fill=1)

        # Labels.
        _center_text(draw, "Pi", 1, _FONT_MED, region_width=64, x_offset=0)
        _center_text(draw, "Case", 1, _FONT_MED, region_width=64, x_offset=64)

        # Dial gauges (percentage of 0-100 °C range).
        _draw_arc_gauge(draw, 32, 34, 16, min(cpu_temp, 100.0))
        _draw_arc_gauge(draw, 96, 34, 16, min(case_temp, 100.0))

        # Numeric readings.
        _center_text(draw, f"{cpu_temp:.0f}C", 48, _FONT_MED, region_width=64, x_offset=0)
        _center_text(draw, f"{case_temp:.0f}C", 48, _FONT_MED, region_width=64, x_offset=64)

        return image

    def _render_fan_duty(self) -> Image.Image:
        """Render the fan duty screen with three dial gauges.

        Three-column layout: Pi (RPi PWM fan), C1, C2.  Each column has a
        label, a sweep-arc dial, and the duty percentage below.
        """
        image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        draw = ImageDraw.Draw(image)

        fan_duty = self._latest_metrics.get("fan_duty", [0, 0, 0])
        if not isinstance(fan_duty, list) or len(fan_duty) < 3:
            fan_duty = [0, 0, 0]

        rpi_fan_duty = self._latest_metrics.get("rpi_fan_duty", 0)

        # Border and column dividers.
        draw.rectangle([0, 0, 127, 63], outline=1)
        draw.line([(43, 0), (43, 63)], fill=1)
        draw.line([(86, 0), (86, 63)], fill=1)

        # Labels.
        _center_text(draw, "Pi", 1, _FONT_SMALL, region_width=43, x_offset=0)
        _center_text(draw, "C1", 1, _FONT_SMALL, region_width=43, x_offset=43)
        _center_text(draw, "C2", 1, _FONT_SMALL, region_width=42, x_offset=86)

        # Compute percentages (hardware range 0-255 for expansion, 0-255 for RPi).
        pi_pct = rpi_fan_duty / 255.0 * 100.0
        c1_pct = fan_duty[0] / 255.0 * 100.0
        c2_pct = fan_duty[1] / 255.0 * 100.0

        # Dial gauges.
        _draw_arc_gauge(draw, 21, 34, 16, pi_pct)
        _draw_arc_gauge(draw, 64, 34, 16, c1_pct)
        _draw_arc_gauge(draw, 107, 34, 16, c2_pct)

        # Percentage labels.
        _center_text(draw, f"{pi_pct:.0f}%", 50, _FONT_SMALL, region_width=43, x_offset=0)
        _center_text(draw, f"{c1_pct:.0f}%", 50, _FONT_SMALL, region_width=43, x_offset=43)
        _center_text(draw, f"{c2_pct:.0f}%", 50, _FONT_SMALL, region_width=42, x_offset=86)

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

    # How often the display re-renders (seconds).  Keeps the clock
    # seconds ticking and metric values fresh while staying on the
    # same screen.
    _RENDER_INTERVAL: float = 1.0

    async def _display_loop(self) -> None:
        """Cycle through enabled screens, re-rendering every second.

        Each screen stays visible for its configured *display_time* but
        is re-drawn every ``_RENDER_INTERVAL`` seconds so that the clock
        ticks, percentage values update, etc.
        """
        logger.info("OLED display loop started")
        import time as _time

        screen_start: float = _time.monotonic()

        while True:
            try:
                config = await self._get_oled_config()

                # If current screen is disabled, advance to next enabled one.
                current_enabled = (
                    self._current_screen < len(config.screens)
                    and config.screens[self._current_screen].enabled
                )

                if not current_enabled:
                    next_screen = self._find_next_enabled_screen(config)
                    if next_screen is None:
                        await asyncio.sleep(1.0)
                        continue
                    self._current_screen = next_screen
                    screen_start = _time.monotonic()

                # Re-render the current screen (picks up fresh metrics
                # and the current time).
                renderer = self._RENDERERS.get(self._current_screen)
                if renderer is not None:
                    image = renderer(self)
                    await self._render_to_display(image)

                # Check whether it's time to advance to the next screen.
                display_time = self._get_screen_display_time(
                    config, self._current_screen,
                )
                elapsed = _time.monotonic() - screen_start
                if elapsed >= display_time:
                    next_screen = self._find_next_enabled_screen(config)
                    if next_screen is not None:
                        self._current_screen = next_screen
                    screen_start = _time.monotonic()

                await asyncio.sleep(self._RENDER_INTERVAL)

            except asyncio.CancelledError:
                logger.info("OLED display loop cancelled")
                raise
            except Exception:
                logger.error("OLED display loop error", exc_info=True)
                self._degraded = True
                await asyncio.sleep(5.0)
