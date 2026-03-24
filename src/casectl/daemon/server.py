"""FastAPI application factory for the casectl daemon.

The :func:`create_app` function builds and configures the FastAPI application
with core health/plugin endpoints, a WebSocket event stream, CORS middleware,
and all plugin-registered routes.  Lifecycle hooks wire into the
:class:`~casectl.daemon.plugin_host.PluginHost` so that plugins are started
and stopped alongside the ASGI server.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

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
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    plugin_host: PluginHost,
    config_manager: ConfigManager,
    event_bus: EventBus,
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

    # -- CORS ---------------------------------------------------------------
    # casectl is a local-network appliance; open CORS makes it easy for
    # dashboards and SPAs served from any origin to talk to the API.

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
            return await config_manager.get(section)
        except KeyError as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(  # type: ignore[return-value]
                status_code=404,
                content={"detail": str(exc)},
            )

    # -- WebSocket ----------------------------------------------------------

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Real-time event stream over WebSocket.

        Clients connect and receive JSON frames for every event emitted on the
        :class:`~casectl.daemon.event_bus.EventBus`.  Client-to-server messages
        are accepted (for keep-alive) but ignored.

        If the maximum number of WebSocket subscribers has been reached the
        connection is rejected with close code ``1008`` (Policy Violation).
        """
        if not event_bus.add_ws_subscriber(websocket):
            await websocket.close(code=1008, reason="Too many connections")
            return

        await websocket.accept()
        logger.debug("WebSocket client connected (%d active)", event_bus.ws_count)

        try:
            while True:
                # Keep the connection alive; ignore any client-sent messages.
                await websocket.receive_text()
        except WebSocketDisconnect:
            logger.debug("WebSocket client disconnected normally")
        except Exception:
            logger.debug("WebSocket connection lost", exc_info=True)
        finally:
            event_bus.remove_ws_subscriber(websocket)
            logger.debug(
                "WebSocket client removed (%d active)", event_bus.ws_count
            )

    # -- Mount plugin routes ------------------------------------------------
    # Plugin routes are prefixed with the key from PluginHost._routes, which
    # is already ``/api/plugins/<name>``.

    for prefix, router in plugin_host.get_routes():
        app.include_router(router, prefix=prefix)
        logger.debug("Mounted plugin routes at %s", prefix)

    return app
