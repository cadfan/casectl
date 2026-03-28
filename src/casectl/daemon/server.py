"""FastAPI application factory for the casectl daemon.

The :func:`create_app` function builds and configures the FastAPI application
with core health/plugin endpoints, a WebSocket event stream, CORS middleware,
and all plugin-registered routes.  Lifecycle hooks wire into the
:class:`~casectl.daemon.plugin_host.PluginHost` so that plugins are started
and stopped alongside the ASGI server.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from casectl.config.manager import ConfigManager
    from casectl.daemon.event_bus import EventBus
    from casectl.daemon.plugin_host import PluginHost

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version — sourced from the package to avoid duplication
# ---------------------------------------------------------------------------

try:
    from casectl import __version__ as _DAEMON_VERSION
except ImportError:
    _DAEMON_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Basic auth middleware
# ---------------------------------------------------------------------------


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth middleware using a bearer token or username:password.

    The token is read from the ``CASECTL_API_TOKEN`` environment variable.
    If not set, auth is disabled (localhost-only mode is safe without it).
    When the API is bound to 0.0.0.0, a token is auto-generated if not set.
    """

    def __init__(self, app: Any, token: str | None = None, trust_proxy: bool = False) -> None:
        super().__init__(app)
        self._token = token
        self._trust_proxy = trust_proxy

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if self._token is None:
            return await call_next(request)

        # Allow localhost connections without auth (CLI runs locally)
        client_host = request.client.host if request.client else ""

        # Check for proxy: if trust_proxy is enabled, use X-Forwarded-For
        forwarded_for = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if self._trust_proxy and forwarded_for:
            effective_host = forwarded_for
            if client_host in ("127.0.0.1", "::1", "localhost") and effective_host not in ("127.0.0.1", "::1", "localhost"):
                pass  # Don't bypass auth — real client is remote
            elif effective_host in ("127.0.0.1", "::1", "localhost"):
                return await call_next(request)  # Real client is local
        else:
            if client_host in ("127.0.0.1", "::1", "localhost"):
                return await call_next(request)

        # Allow static assets without auth (CSS/JS only)
        if request.url.path.startswith("/static/"):
            return await call_next(request)

        # Check query parameter first (for browser access — avoids Basic Auth prompt)
        token_param = request.query_params.get("token")
        if token_param and secrets.compare_digest(token_param, self._token):
            return await call_next(request)

        # Check cookie (set after successful token auth for subsequent requests)
        cookie_token = request.cookies.get("casectl_token")
        if cookie_token and secrets.compare_digest(cookie_token, self._token):
            return await call_next(request)

        # Check Authorization header
        auth = request.headers.get("Authorization", "")

        # Support "Bearer <token>" format
        if auth.startswith("Bearer "):
            provided = auth[7:].strip()
            if secrets.compare_digest(provided, self._token):
                return await call_next(request)

        # Support HTTP Basic Auth (username ignored, password = token)
        if auth.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                _, _, password = decoded.partition(":")
                if secrets.compare_digest(password, self._token):
                    return await call_next(request)
            except Exception:
                pass

        return Response(
            content='{"detail":"Unauthorized — provide token via ?token= query param, cookie, or Authorization header"}',
            status_code=401,
            media_type="application/json",
            headers={"WWW-Authenticate": 'Bearer realm="casectl"'},
        )


def _resolve_api_token(host: str) -> str | None:
    """Determine the API token to use.

    - If CASECTL_API_TOKEN env var is set, use it.
    - If binding to a non-localhost address, auto-generate and save a token.
    - If localhost-only, no token needed.
    """
    token = os.environ.get("CASECTL_API_TOKEN")
    if token:
        return token

    # Auto-generate token for any non-localhost bind
    _LOCALHOST = {"127.0.0.1", "::1", "localhost"}
    if host not in _LOCALHOST:
        token = secrets.token_urlsafe(24)

        # Write full token to file (0o600)
        from pathlib import Path
        token_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "casectl"
        token_dir.mkdir(parents=True, exist_ok=True)
        token_path = token_dir / ".api-token"
        token_path.write_text(token)
        token_path.chmod(0o600)

        logger.warning(
            "API bound to %s with auto-generated token: %s...",
            host, token[:8],
        )
        logger.warning("Full token written to %s", token_path)
        logger.warning("Dashboard: http://<pi-ip>:%s/?token=<see token file>",
                       os.environ.get("CASECTL_API_PORT", "8420"))
        return token

    return None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class PatchConfigRequest(BaseModel):
    """Request body for PATCH /api/config."""

    section: str = Field(description="Config section to update (fan, led, oled, service, alerts)")
    values: dict[str, Any] = Field(description="Key-value pairs to merge into the section")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    plugin_host: PluginHost,
    config_manager: ConfigManager,
    event_bus: EventBus,
    host: str = "127.0.0.1",
    port: int = 8420,
    trust_proxy: bool = False,
) -> FastAPI:
    """Build and return a fully-configured :class:`FastAPI` application.

    Parameters
    ----------
    plugin_host:
        The :class:`~casectl.daemon.plugin_host.PluginHost` that manages
        plugin lifecycles and routes.
    config_manager:
        The :class:`~casectl.config.manager.ConfigManager` for configuration
        access.
    event_bus:
        The :class:`~casectl.daemon.event_bus.EventBus` used for real-time
        event subscriptions and WebSocket broadcasting.

    Returns
    -------
    FastAPI
        A ready-to-serve ASGI application.
    """

    # -- Lifecycle manager --------------------------------------------------
    # FastAPI >= 0.93 uses the ``lifespan`` context manager instead of
    # ``on_event("startup")`` / ``on_event("shutdown")``.

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start plugins on startup, stop them (and emit events) on shutdown."""
        logger.info("casectl lifespan: starting plugins")
        try:
            await plugin_host.start_all()
            await event_bus.emit("daemon.started", {"version": _DAEMON_VERSION})
        except Exception:
            logger.error("Error during plugin startup", exc_info=True)
            # Continue — individual plugin errors are already caught by
            # PluginHost.start_all(); this catches truly unexpected failures.

        yield  # Application is serving requests

        logger.info("casectl lifespan: stopping plugins")
        try:
            await event_bus.emit("daemon.stopping", {})
            await plugin_host.stop_all()
        except Exception:
            logger.error("Error during plugin shutdown", exc_info=True)

    # -- Application --------------------------------------------------------

    app = FastAPI(
        title="casectl",
        version=_DAEMON_VERSION,
        description="Headless-first controller for Freenove FNK0107B case hardware",
        lifespan=_lifespan,
    )

    # -- Authentication -----------------------------------------------------
    # Auto-generates a token when bound to 0.0.0.0 (LAN access).

    api_token = _resolve_api_token(host)
    if api_token:
        app.add_middleware(BasicAuthMiddleware, token=api_token, trust_proxy=trust_proxy)
        app.state.api_token = api_token
    else:
        app.state.api_token = None

    # -- CORS ---------------------------------------------------------------
    if api_token:
        # LAN mode: restrict origins to the daemon's own address.
        _origins = [
            f"http://{host}:{port}",
            f"https://{host}:{port}",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )
    else:
        # Localhost only: open CORS is acceptable.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # -- Request timing -----------------------------------------------------

    start_time: float = time.time()

    # ======================================================================
    # Core endpoints (not provided by plugins)
    # ======================================================================

    @app.get("/api/health", tags=["core"])
    async def health() -> dict[str, Any]:
        """Return daemon health, uptime, version, and plugin summary."""
        return {
            "status": "running",
            "uptime": int(time.time() - start_time),
            "version": _DAEMON_VERSION,
            "api_version": "0.1",
            "plugins": plugin_host.list_plugins(),
        }

    @app.get("/api/plugins", tags=["core"])
    async def list_plugins() -> list[dict[str, Any]]:
        """Return summary information for every loaded plugin."""
        return plugin_host.list_plugins()

    @app.get("/api/config/{section}", tags=["core"])
    async def get_config_section(section: str) -> dict[str, Any]:
        """Return a single configuration section as a plain dict.

        Parameters
        ----------
        section:
            Top-level key in ``config.yaml`` (e.g. ``fan``, ``led``, ``oled``).
        """
        try:
            data = await config_manager.get(section)
            if section == "alerts" and isinstance(data, dict):
                data = {**data, "smtp_password": "***"} if "smtp_password" in data else data
            return data
        except KeyError as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(  # type: ignore[return-value]
                status_code=404,
                content={"detail": str(exc)},
            )

    @app.patch("/api/config", tags=["core"])
    async def patch_config(body: PatchConfigRequest) -> dict[str, Any]:
        """Update a configuration section with partial data.

        The request body must include a ``section`` key identifying the
        top-level config section to update, and a ``values`` dict of
        key-value pairs to merge into that section.

        Emits a ``config.updated`` event on the EventBus after a successful
        update so that SSE/WebSocket subscribers are notified immediately
        (enabling <500 ms dashboard round-trip).
        """
        section = body.section
        if not section:
            raise HTTPException(status_code=400, detail="Missing 'section' key")
        try:
            updated = await config_manager.update(section, body.values)
            # Emit config change event for real-time push to dashboards.
            await event_bus.emit("config.updated", {
                "section": section,
                "values": body.values,
                "ts": time.time(),
            })
            return updated.model_dump(mode="json")
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown config section")
        except Exception:
            logger.error("Failed to update config", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to update configuration")

    # -- WebSocket command helpers ------------------------------------------

    async def _handle_ws_command(websocket: WebSocket, raw: str) -> None:
        """Parse and execute a WebSocket command message.

        Accepted commands mirror the REST API so that the web dashboard has
        full CLI parity over the WebSocket channel:

        - ``{"command": "set_fan_mode", "mode": <int|str>}``
        - ``{"command": "set_led_mode", "mode": <int|str>}``
        - ``{"command": "set_led_color", "red": int, "green": int, "blue": int}``
        - ``{"command": "set_fan_speed", "duty": [int, ...]}``
        - ``{"command": "set_oled_screen", "index": int, "enabled": bool}``
        - ``{"command": "set_oled_rotation", "rotation": 0|180}``
        - ``{"command": "set_oled_power", "enabled": bool}``
        - ``{"command": "set_oled_content", "screen_index": int, ...}``

        Each command invokes the same ``config_manager.update()`` path used by
        the REST API and CLI, ensuring behavioural parity across all interfaces.
        """
        import json as _json

        try:
            msg = _json.loads(raw)
        except (ValueError, TypeError):
            await websocket.send_text(_json.dumps({
                "type": "error", "data": {"detail": "Invalid JSON"},
            }))
            return

        if not isinstance(msg, dict) or "command" not in msg:
            # Keep-alive / ping — silently ignore.
            return

        command = msg["command"]
        try:
            result = await _dispatch_ws_command(command, msg)
            await websocket.send_text(_json.dumps({
                "type": "command_result",
                "data": {"command": command, "status": "ok", **result},
            }))
        except ValueError as exc:
            await websocket.send_text(_json.dumps({
                "type": "error",
                "data": {"command": command, "detail": str(exc)},
            }))
        except Exception:
            logger.error("WebSocket command %r failed", command, exc_info=True)
            await websocket.send_text(_json.dumps({
                "type": "error",
                "data": {"command": command, "detail": "Internal server error"},
            }))

    async def _dispatch_ws_command(command: str, msg: dict[str, Any]) -> dict[str, Any]:
        """Route a WebSocket command to the appropriate config_manager call.

        Returns a result dict on success, raises ValueError on bad input.

        Each command invokes ``config_manager.update()`` (the same path used
        by the REST API and CLI) and then emits the appropriate event on the
        :class:`~casectl.daemon.event_bus.EventBus` so that SSE clients and
        other WebSocket subscribers receive real-time notifications.
        """
        from casectl.config.models import FanMode, LedMode

        if command == "set_fan_mode":
            mode_raw = msg.get("mode")
            if mode_raw is None:
                raise ValueError("Missing 'mode' parameter")
            mode_val = _resolve_fan_mode(mode_raw)
            await config_manager.update("fan", {"mode": mode_val})
            mode_name = FanMode(mode_val).name.lower()
            await event_bus.emit("fan.mode_changed", {
                "mode": mode_name,
                "mode_value": mode_val,
                "source": "websocket",
                "ts": time.time(),
            })
            return {"mode": mode_name}

        elif command == "set_led_mode":
            mode_raw = msg.get("mode")
            if mode_raw is None:
                raise ValueError("Missing 'mode' parameter")
            mode_val = _resolve_led_mode(mode_raw)
            await config_manager.update("led", {"mode": mode_val})
            mode_name = LedMode(mode_val).name.lower()
            await event_bus.emit("led.mode_changed", {
                "mode": mode_name,
                "mode_value": mode_val,
                "source": "websocket",
                "ts": time.time(),
            })
            return {"mode": mode_name}

        elif command == "set_led_color":
            red, green, blue = _resolve_led_color(msg)
            await config_manager.update("led", {
                "mode": LedMode.MANUAL.value,
                "red_value": red,
                "green_value": green,
                "blue_value": blue,
            })
            await event_bus.emit("led.color_changed", {
                "color": {"red": red, "green": green, "blue": blue},
                "source": "websocket",
                "ts": time.time(),
            })
            return {"color": {"red": red, "green": green, "blue": blue}}

        elif command == "set_fan_speed":
            duty = msg.get("duty")
            if not isinstance(duty, list) or not (1 <= len(duty) <= 3):
                raise ValueError("duty must be a list of 1-3 integers (0-100)")
            hw_duty: list[int] = []
            for d in duty:
                if not isinstance(d, int) or not (0 <= d <= 100):
                    raise ValueError("Each duty value must be 0-100")
                hw_duty.append(int(d * 255 / 100))
            while len(hw_duty) < 3:
                hw_duty.append(hw_duty[-1] if hw_duty else 0)
            await config_manager.update("fan", {
                "mode": FanMode.MANUAL.value,
                "manual_duty": hw_duty,
            })
            await event_bus.emit("fan.speed_changed", {
                "duty_hw": hw_duty,
                "source": "websocket",
                "ts": time.time(),
            })
            return {"duty_hw": hw_duty}

        # -- OLED commands --------------------------------------------------

        elif command == "set_oled_screen":
            index = msg.get("index")
            enabled = msg.get("enabled")
            if not isinstance(index, int) or not (0 <= index <= 3):
                raise ValueError("index must be an integer 0-3")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")

            raw_oled = await config_manager.get("oled")
            screens = raw_oled.get("screens", [])
            if index >= len(screens):
                raise ValueError(f"Screen index {index} out of range (0-{len(screens) - 1})")

            screens[index]["enabled"] = enabled
            await config_manager.update("oled", {"screens": screens})
            await event_bus.emit("oled.screen_toggled", {
                "index": index,
                "enabled": enabled,
                "source": "websocket",
                "ts": time.time(),
            })
            return {"index": index, "enabled": enabled}

        elif command == "set_oled_rotation":
            rotation = msg.get("rotation")
            if rotation not in (0, 180):
                raise ValueError("rotation must be 0 or 180")

            await config_manager.update("oled", {"rotation": rotation})
            await event_bus.emit("oled.rotation_changed", {
                "rotation": rotation,
                "source": "websocket",
                "ts": time.time(),
            })
            return {"rotation": rotation}

        elif command == "set_oled_power":
            enabled = msg.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")

            # Enable or disable all screens at once — matches CLI `casectl oled on/off`.
            raw_oled = await config_manager.get("oled")
            screens = raw_oled.get("screens", [])
            for screen in screens:
                screen["enabled"] = enabled
            await config_manager.update("oled", {"screens": screens})
            await event_bus.emit("oled.power_changed", {
                "enabled": enabled,
                "source": "websocket",
                "ts": time.time(),
            })
            return {"enabled": enabled}

        elif command == "set_oled_content":
            screen_index = msg.get("screen_index")
            if not isinstance(screen_index, int) or not (0 <= screen_index <= 3):
                raise ValueError("screen_index must be an integer 0-3")

            raw_oled = await config_manager.get("oled")
            screens = raw_oled.get("screens", [])
            if screen_index >= len(screens):
                raise ValueError(
                    f"Screen index {screen_index} out of range (0-{len(screens) - 1})"
                )

            # Apply optional per-screen content settings.
            updated_screen = dict(screens[screen_index])
            if "display_time" in msg:
                dt = msg["display_time"]
                if not isinstance(dt, (int, float)) or dt <= 0:
                    raise ValueError("display_time must be a positive number")
                updated_screen["display_time"] = float(dt)
            if "time_format" in msg:
                tf = msg["time_format"]
                if tf not in (0, 1):
                    raise ValueError("time_format must be 0 (24h) or 1 (12h)")
                updated_screen["time_format"] = tf
            if "date_format" in msg:
                df = msg["date_format"]
                if not isinstance(df, int) or df < 0:
                    raise ValueError("date_format must be a non-negative integer")
                updated_screen["date_format"] = df
            if "interchange" in msg:
                ic = msg["interchange"]
                if not isinstance(ic, int) or ic < 0:
                    raise ValueError("interchange must be a non-negative integer")
                updated_screen["interchange"] = ic

            screens[screen_index] = updated_screen
            await config_manager.update("oled", {"screens": screens})
            await event_bus.emit("oled.content_changed", {
                "screen_index": screen_index,
                "settings": updated_screen,
                "source": "websocket",
                "ts": time.time(),
            })
            return {"screen_index": screen_index, "settings": updated_screen}

        else:
            raise ValueError(f"Unknown command: {command}")

    def _resolve_fan_mode(raw: int | str) -> int:
        """Convert a fan mode name or integer to a valid FanMode int value."""
        _FAN_NAMES = {
            "follow-temp": 0, "follow_temp": 0,
            "follow-rpi": 1, "follow_rpi": 1,
            "manual": 2, "custom": 3, "off": 4,
        }
        if isinstance(raw, str):
            val = _FAN_NAMES.get(raw.lower())
            if val is None:
                raise ValueError(f"Unknown fan mode: {raw}")
            return val
        if isinstance(raw, int) and raw in range(5):
            return raw
        raise ValueError(f"Invalid fan mode: {raw}")

    def _resolve_led_mode(raw: int | str) -> int:
        """Convert an LED mode name or integer to a valid LedMode int value."""
        _LED_NAMES = {
            "rainbow": 0, "breathing": 1,
            "follow-temp": 2, "follow_temp": 2,
            "manual": 3, "custom": 4, "off": 5,
        }
        if isinstance(raw, str):
            val = _LED_NAMES.get(raw.lower())
            if val is None:
                raise ValueError(f"Unknown LED mode: {raw}")
            return val
        if isinstance(raw, int) and raw in range(6):
            return raw
        raise ValueError(f"Invalid LED mode: {raw}")

    # Named colour map — matches CLI `casectl led color <name>` and
    # the REST API's _COLOR_NAMES in led/routes.py.
    _WS_COLOR_NAMES: dict[str, tuple[int, int, int]] = {
        "red": (255, 0, 0),
        "green": (0, 255, 0),
        "blue": (0, 0, 255),
        "white": (255, 255, 255),
        "yellow": (255, 255, 0),
        "cyan": (0, 255, 255),
        "magenta": (255, 0, 255),
        "orange": (255, 165, 0),
        "pink": (255, 105, 180),
        "purple": (128, 0, 128),
        "teal": (0, 128, 128),
        "coral": (255, 127, 80),
        "gold": (255, 215, 0),
        "lime": (0, 255, 0),
        "navy": (0, 0, 128),
        "arctic-steel": (138, 170, 196),
    }

    def _resolve_led_color(msg: dict[str, Any]) -> tuple[int, int, int]:
        """Resolve LED colour from a WebSocket message.

        Supports three input formats matching the CLI:

        1. **Named colour**: ``{"color_name": "red"}``
        2. **Hex code**: ``{"hex": "#FF0080"}``
        3. **RGB values**: ``{"red": 255, "green": 0, "blue": 128}``

        Returns a (red, green, blue) tuple.  Raises ``ValueError`` on
        invalid input.
        """
        # 1. Named colour (highest priority — matches `casectl led color red`)
        color_name = msg.get("color_name")
        if color_name and isinstance(color_name, str):
            rgb = _WS_COLOR_NAMES.get(color_name.lower())
            if rgb is None:
                raise ValueError(
                    f"Unknown colour name: {color_name}. "
                    f"Valid: {', '.join(sorted(_WS_COLOR_NAMES))}"
                )
            return rgb

        # 2. Hex code (matches `casectl led color #FF0080`)
        hex_code = msg.get("hex")
        if hex_code and isinstance(hex_code, str):
            hex_str = hex_code.lstrip("#")
            if len(hex_str) != 6:
                raise ValueError(f"Hex colour must be 6 digits, got: {hex_code}")
            try:
                r = int(hex_str[0:2], 16)
                g = int(hex_str[2:4], 16)
                b = int(hex_str[4:6], 16)
                return (r, g, b)
            except ValueError:
                raise ValueError(f"Invalid hex colour: {hex_code}")

        # 3. RGB values (matches `casectl led color 255 0 128`)
        red = msg.get("red", 0)
        green = msg.get("green", 0)
        blue = msg.get("blue", 0)
        for name, val in [("red", red), ("green", green), ("blue", blue)]:
            if not isinstance(val, int) or not (0 <= val <= 255):
                raise ValueError(f"{name} must be an integer 0-255")
        return (red, green, blue)

    # -- WebSocket ----------------------------------------------------------

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Bidirectional WebSocket for real-time events and dashboard commands.

        **Server → Client:** JSON frames for every event emitted on the
        :class:`~casectl.daemon.event_bus.EventBus`.

        **Client → Server:** JSON command messages that invoke the same core
        functions as the CLI and REST API, providing full CLI parity over the
        WebSocket channel.  Supported commands:

        - ``{"command": "set_fan_mode", "mode": <int|str>}``
        - ``{"command": "set_led_mode", "mode": <int|str>}``
        - ``{"command": "set_led_color", "red": int, "green": int, "blue": int}``
        - ``{"command": "set_fan_speed", "duty": [int, ...]}``

        Non-command messages (no ``"command"`` key) are treated as keep-alive
        pings and silently ignored.

        Authentication is required when the daemon has an API token.
        Pass the token as a query parameter: ``ws://host:port/api/ws?token=...``
        """
        # Authenticate WebSocket if token is set.
        ws_api_token = getattr(app.state, "api_token", None)
        if ws_api_token:
            client_host = websocket.client.host if websocket.client else ""
            if client_host not in ("127.0.0.1", "::1", "localhost"):
                ws_token = websocket.query_params.get("token", "")
                if not ws_token or not secrets.compare_digest(ws_token, ws_api_token):
                    await websocket.accept()
                    await websocket.close(code=1008, reason="Authentication required")
                    return

        await websocket.accept()
        if not event_bus.add_ws_subscriber(websocket):
            await websocket.close(code=1008, reason="Too many connections")
            return
        logger.debug("WebSocket client connected (%d active)", event_bus.ws_count)

        try:
            while True:
                raw = await websocket.receive_text()
                await _handle_ws_command(websocket, raw)
        except WebSocketDisconnect:
            logger.debug("WebSocket client disconnected normally")
        except Exception:
            logger.debug("WebSocket connection lost", exc_info=True)
        finally:
            event_bus.remove_ws_subscriber(websocket)
            logger.debug(
                "WebSocket client removed (%d active)", event_bus.ws_count
            )

    # -- Populate app.state for plugin Depends() injection -------------------
    plugin_host.populate_app_state(app)

    # -- Mount plugin routes ------------------------------------------------
    # Plugin routes are prefixed with the key from PluginHost._routes, which
    # is already ``/api/plugins/<name>``.

    for prefix, router in plugin_host.get_routes():
        app.include_router(router, prefix=prefix)
        logger.debug("Mounted plugin routes at %s", prefix)

    # -- Mount SSE real-time endpoint ----------------------------------------
    try:
        from casectl.web.sse import create_sse_router

        sse_router, sse_manager = create_sse_router(event_bus)
        app.include_router(sse_router)
        app.state.sse_manager = sse_manager
        logger.info("SSE real-time endpoint mounted at /api/sse")
    except Exception:
        logger.warning("Failed to mount SSE endpoint", exc_info=True)

    # -- Mount web dashboard ------------------------------------------------
    try:
        from casectl.web.app import create_web_router

        web_router = create_web_router(plugin_host, config_manager)
        app.include_router(web_router)
        logger.info("Web dashboard mounted at /")
    except Exception:
        logger.warning("Failed to mount web dashboard", exc_info=True)

    return app
