"""FastAPI router for the casectl web dashboard.

Serves Jinja2 templates and static files for the browser-based dashboard.
Each partial route fetches live data from the plugin APIs and renders an
HTMX-compatible HTML fragment.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from casectl.config.manager import ConfigManager
    from casectl.daemon.plugin_host import PluginHost

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _hostname() -> str:
    """Return the system hostname, or a fallback."""
    try:
        return socket.gethostname()
    except Exception:
        return "casectl"


async def _fetch_monitor_data(plugin_host: PluginHost) -> dict[str, Any]:
    """Fetch latest system metrics from the system-monitor plugin."""
    plugin = plugin_host.get_plugin("system-monitor")
    if plugin is None:
        return {}
    try:
        status = plugin.get_status()
        return status.get("metrics", {})
    except Exception:
        logger.debug("Failed to fetch monitor data", exc_info=True)
        return {}


async def _fetch_fan_data(plugin_host: PluginHost) -> dict[str, Any]:
    """Fetch fan status from the fan-control plugin."""
    plugin = plugin_host.get_plugin("fan-control")
    if plugin is None:
        return {"mode": "unknown", "duty": [0, 0, 0], "rpm": [0, 0, 0], "degraded": False}
    try:
        status = plugin.get_status()
        return {
            "mode": status.get("mode", "unknown"),
            "duty": status.get("duty", [0, 0, 0]),
            "rpm": [0, 0, 0],  # RPM requires hardware read, use metrics if available
            "degraded": status.get("degraded", False),
        }
    except Exception:
        logger.debug("Failed to fetch fan data", exc_info=True)
        return {"mode": "unknown", "duty": [0, 0, 0], "rpm": [0, 0, 0], "degraded": False}


async def _fetch_led_data(plugin_host: PluginHost) -> dict[str, Any]:
    """Fetch LED status from the led-control plugin."""
    plugin = plugin_host.get_plugin("led-control")
    if plugin is None:
        return {"mode": "unknown", "color": {"red": 0, "green": 0, "blue": 0}, "degraded": False}
    try:
        status = plugin.get_status()
        return {
            "mode": status.get("mode", "unknown"),
            "color": status.get("color", {"red": 0, "green": 0, "blue": 0}),
            "degraded": status.get("degraded", False),
        }
    except Exception:
        logger.debug("Failed to fetch LED data", exc_info=True)
        return {"mode": "unknown", "color": {"red": 0, "green": 0, "blue": 0}, "degraded": False}


async def _fetch_oled_data(plugin_host: PluginHost) -> dict[str, Any]:
    """Fetch OLED status from the oled-display plugin."""
    plugin = plugin_host.get_plugin("oled-display")
    if plugin is None:
        return {
            "current_screen": 0,
            "screen_names": ["clock", "metrics", "temperature", "fan_duty"],
            "screens_enabled": [True, True, True, True],
            "rotation": 0,
            "degraded": False,
        }
    try:
        status = plugin.get_status()
        # The plugin get_status() may not include screens_enabled; try the
        # internal status accessor if the plugin exposes it.
        if hasattr(plugin, "_get_oled_status"):
            detailed = plugin._get_oled_status()
            return detailed
        return {
            "current_screen": status.get("current_screen", 0),
            "screen_names": status.get("screen_names", ["clock", "metrics", "temperature", "fan_duty"]),
            "screens_enabled": status.get("screens_enabled", [True, True, True, True]),
            "rotation": status.get("rotation", 0),
            "degraded": status.get("degraded", False),
        }
    except Exception:
        logger.debug("Failed to fetch OLED data", exc_info=True)
        return {
            "current_screen": 0,
            "screen_names": ["clock", "metrics", "temperature", "fan_duty"],
            "screens_enabled": [True, True, True, True],
            "rotation": 0,
            "degraded": False,
        }


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_web_router(plugin_host: PluginHost, config_manager: ConfigManager) -> APIRouter:
    """Create and return the web dashboard router.

    Parameters
    ----------
    plugin_host:
        The :class:`~casectl.daemon.plugin_host.PluginHost` for fetching
        plugin data.
    config_manager:
        The :class:`~casectl.config.manager.ConfigManager` for reading
        configuration values.

    Returns
    -------
    APIRouter
        A FastAPI router with all web dashboard routes and static file
        serving configured.
    """
    router = APIRouter()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # -----------------------------------------------------------------------
    # Full-page routes
    # -----------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        """Render the main dashboard page."""
        hostname = _hostname()

        # Fetch initial data for all cards so the first render is populated.
        metrics = await _fetch_monitor_data(plugin_host)
        fan = await _fetch_fan_data(plugin_host)
        led = await _fetch_led_data(plugin_host)
        oled = await _fetch_oled_data(plugin_host)

        # Determine overall health from plugin statuses.
        all_statuses = plugin_host.get_all_statuses()
        health = "healthy"
        for _name, status in all_statuses.items():
            if status.value == "error":
                health = "error"
                break
            if status.value == "degraded":
                health = "degraded"

        response = templates.TemplateResponse(request, "dashboard.html",
            {
                "hostname": hostname,
                "health": health,
                # Monitor data
                "cpu_temp": metrics.get("cpu_temp", 0.0),
                "cpu_percent": metrics.get("cpu_percent", 0.0),
                "memory_percent": metrics.get("memory_percent", 0.0),
                "disk_percent": metrics.get("disk_percent", 0.0),
                "ip_address": metrics.get("ip_address", ""),
                "case_temp": metrics.get("case_temp", 0.0),
                # Fan data
                "fan_mode": fan.get("mode", "unknown"),
                "fan_duty": fan.get("duty", [0, 0, 0]),
                "fan_rpm": fan.get("rpm", [0, 0, 0]),
                # LED data
                "led_mode": led.get("mode", "unknown"),
                "led_color": led.get("color", {"red": 0, "green": 0, "blue": 0}),
                # OLED data
                "oled_current_screen": oled.get("current_screen", 0),
                "oled_screen_names": oled.get("screen_names", []),
                "oled_screens_enabled": oled.get("screens_enabled", []),
            },
        )

        # Set auth cookie so HTMX partial requests authenticate without ?token=
        token = request.query_params.get("token")
        if token:
            response.set_cookie("casectl_token", token, httponly=True, samesite="lax")

        return response

    # -----------------------------------------------------------------------
    # HTMX partial routes
    # -----------------------------------------------------------------------

    @router.get("/w/monitor", response_class=HTMLResponse)
    async def partial_monitor(request: Request) -> HTMLResponse:
        """Render the system monitor HTMX partial."""
        metrics = await _fetch_monitor_data(plugin_host)
        return templates.TemplateResponse(request, "partials/monitor.html",
            {
                "cpu_temp": metrics.get("cpu_temp", 0.0),
                "cpu_percent": metrics.get("cpu_percent", 0.0),
                "memory_percent": metrics.get("memory_percent", 0.0),
                "disk_percent": metrics.get("disk_percent", 0.0),
                "ip_address": metrics.get("ip_address", ""),
                "case_temp": metrics.get("case_temp", 0.0),
            },
        )

    @router.get("/w/fan", response_class=HTMLResponse)
    async def partial_fan(request: Request) -> HTMLResponse:
        """Render the fan control HTMX partial."""
        fan = await _fetch_fan_data(plugin_host)

        # Also pull motor speeds from monitor metrics if available.
        metrics = await _fetch_monitor_data(plugin_host)
        rpm = metrics.get("motor_speed", [0, 0, 0])
        duty = fan.get("duty", [0, 0, 0])

        return templates.TemplateResponse(request, "partials/fan.html",
            {
                "fan_mode": fan.get("mode", "unknown"),
                "fan_duty": duty,
                "fan_rpm": rpm,
            },
        )

    @router.get("/w/led", response_class=HTMLResponse)
    async def partial_led(request: Request) -> HTMLResponse:
        """Render the LED control HTMX partial."""
        led = await _fetch_led_data(plugin_host)
        return templates.TemplateResponse(request, "partials/led.html",
            {
                "led_mode": led.get("mode", "unknown"),
                "led_color": led.get("color", {"red": 0, "green": 0, "blue": 0}),
            },
        )

    @router.get("/w/oled", response_class=HTMLResponse)
    async def partial_oled(request: Request) -> HTMLResponse:
        """Render the OLED config HTMX partial."""
        oled = await _fetch_oled_data(plugin_host)
        return templates.TemplateResponse(request, "partials/oled.html",
            {
                "oled_current_screen": oled.get("current_screen", 0),
                "oled_screen_names": oled.get("screen_names", []),
                "oled_screens_enabled": oled.get("screens_enabled", []),
            },
        )

    return router
