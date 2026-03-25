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

        # Allow WebSocket upgrade without auth header (handled at WS level)
        if request.url.path == "/api/ws":
            return await call_next(request)

        # Allow static assets and HTMX partials without auth
        if request.url.path.startswith("/static/") or request.url.path.startswith("/w/"):
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

    # -- CORS ---------------------------------------------------------------
    # casectl is a local-network appliance; open CORS makes it easy for
    # dashboards and SPAs served from any origin to talk to the API.

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- Authentication -----------------------------------------------------
    # Auto-generates a token when bound to 0.0.0.0 (LAN access).
    # Token is logged to stdout so the user can see it.

    api_token = _resolve_api_token(host)
    if api_token:
        app.add_middleware(BasicAuthMiddleware, token=api_token, trust_proxy=trust_proxy)
        app.state.api_token = api_token
    else:
        app.state.api_token = None

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
        """
        section = body.section
        if not section:
            raise HTTPException(status_code=400, detail="Missing 'section' key")
        try:
            updated = await config_manager.update(section, body.values)
            return updated.model_dump(mode="json")
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown config section")
        except Exception:
            logger.error("Failed to update config", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to update configuration")

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
        await websocket.accept()
        if not event_bus.add_ws_subscriber(websocket):
            await websocket.close(code=1008, reason="Too many connections")
            return
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

    # -- Mount web dashboard ------------------------------------------------
    try:
        from casectl.web.app import create_web_router

        web_router = create_web_router(plugin_host, config_manager)
        app.include_router(web_router)
        logger.info("Web dashboard mounted at /")
    except Exception:
        logger.warning("Failed to mount web dashboard", exc_info=True)

    return app
