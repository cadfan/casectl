"""Example casectl plugin demonstrating the full plugin API.

This module shows how to build a community plugin for casectl.  It implements
the :class:`~casectl.plugins.base.CasePlugin` protocol and demonstrates:

- Registering an API route via :meth:`PluginContext.register_routes`
- Subscribing to event bus events via :meth:`PluginContext.on_event`
- Reporting plugin health via :meth:`get_status`
- Running background work in :meth:`start` and cleaning up in :meth:`stop`

Install the plugin with ``pip install -e .`` from this directory, and casectl
will discover it automatically via the ``casectl.plugins`` entry-point group
defined in ``pyproject.toml``.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from casectl.plugins.base import PluginContext, PluginStatus


class ExamplePlugin:
    """A minimal but complete casectl plugin.

    This plugin registers a single ``GET /status`` API route and subscribes to
    the ``metrics_updated`` event so it can log system metrics as they arrive.

    Attributes
    ----------
    name : str
        Unique identifier used as the URL prefix and config section name.
        Routes are mounted at ``/api/plugins/{name}/...``.
    version : str
        SemVer version string for this plugin.
    description : str
        One-line human-readable description shown in ``casectl`` status output.
    min_daemon_version : str
        Minimum casectl daemon version required.  The plugin host will skip
        this plugin if the running daemon is older than this value.
    """

    name: str = "example"
    version: str = "0.1.0"
    description: str = "Example plugin demonstrating the casectl plugin API"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._start_time: float | None = None
        self._metrics_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle: setup
    # ------------------------------------------------------------------

    async def setup(self, ctx: PluginContext) -> None:
        """Receive the plugin context and register routes and event handlers.

        This is the first lifecycle method called by the plugin host.  Use it
        to register everything your plugin needs:

        - **API routes** via ``ctx.register_routes(router)``
        - **Config schema** via ``ctx.register_config(MyConfigModel)``
        - **CLI commands** via ``ctx.register_commands(my_click_group)``
        - **Event subscriptions** via ``ctx.on_event("event.name", handler)``

        Do NOT start background tasks here -- that belongs in :meth:`start`.

        Parameters
        ----------
        ctx:
            The :class:`~casectl.plugins.base.PluginContext` for this plugin.
            It provides access to config, hardware, events, and route
            registration.  Store it for later use.
        """
        self._ctx = ctx

        # -- Register API routes -----------------------------------------------
        # Create a FastAPI APIRouter with your endpoints.  The plugin host will
        # mount it at /api/plugins/example/ automatically.
        router = APIRouter(tags=["example"])

        @router.get("/status")
        async def status() -> dict[str, Any]:
            """Return a friendly status message and uptime.

            This endpoint is accessible at:
                GET /api/plugins/example/status
            """
            uptime = int(time.time() - self._start_time) if self._start_time else 0
            return {
                "message": "Hello from the example plugin!",
                "uptime_seconds": uptime,
                "metrics_received": self._metrics_count,
            }

        ctx.register_routes(router)

        # -- Subscribe to events -----------------------------------------------
        # The system-monitor plugin emits "metrics_updated" every 2 seconds
        # with CPU, memory, disk, and temperature data.  Any plugin can
        # subscribe to react to these updates.
        ctx.on_event("metrics_updated", self._on_metrics_updated)

        ctx.logger.info("Example plugin setup complete")

    # ------------------------------------------------------------------
    # Lifecycle: start
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin plugin operation.

        Called by the plugin host after ALL plugins have been set up and the
        HTTP server is ready to accept requests.  Use this to launch background
        tasks (``asyncio.create_task``), open connections, or perform any work
        that depends on the full system being available.

        For this example we simply record the start time used by the /status
        endpoint to report uptime.
        """
        self._start_time = time.time()

        if self._ctx is not None:
            self._ctx.logger.info("Example plugin started")

    # ------------------------------------------------------------------
    # Lifecycle: stop
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Clean up and release resources.

        Called when the daemon is shutting down.  Cancel any background tasks,
        close connections, flush buffers, etc.  The plugin host calls stop() in
        reverse load order, so plugins that depend on others will be stopped
        before their dependencies.
        """
        self._start_time = None
        self._metrics_count = 0

        if self._ctx is not None:
            self._ctx.logger.info("Example plugin stopped")

    # ------------------------------------------------------------------
    # Health reporting
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return plugin health and diagnostic information.

        The plugin host calls this to report plugin status in the dashboard and
        CLI.  The returned dict MUST include a ``"status"`` key with a
        :class:`~casectl.plugins.base.PluginStatus` value.  Additional keys are
        plugin-specific and appear in the health API response.

        Returns
        -------
        dict
            At minimum ``{"status": PluginStatus}``.  This example also
            includes uptime and the count of metrics events received.
        """
        if self._start_time is None:
            return {
                "status": PluginStatus.STOPPED,
                "uptime_seconds": 0,
                "metrics_received": 0,
            }

        return {
            "status": PluginStatus.HEALTHY,
            "uptime_seconds": int(time.time() - self._start_time),
            "metrics_received": self._metrics_count,
        }

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_metrics_updated(self, data: Any) -> None:
        """Handle ``metrics_updated`` events from the system monitor plugin.

        The event bus delivers events to async handlers by awaiting them and to
        sync handlers by calling them directly.  Handler signatures always
        receive a single ``data`` argument containing the event payload.

        For ``metrics_updated``, the payload is a dict matching the
        :class:`~casectl.config.models.SystemMetrics` schema with keys like
        ``cpu_percent``, ``cpu_temp``, ``memory_percent``, etc.

        Parameters
        ----------
        data:
            The event payload -- a dict of system metrics for
            ``metrics_updated``, or any type depending on the event.
        """
        self._metrics_count += 1

        if self._ctx is not None and isinstance(data, dict):
            cpu = data.get("cpu_percent", 0)
            temp = data.get("cpu_temp", 0)
            self._ctx.logger.debug(
                "Metrics update #%d: CPU=%.1f%% temp=%.1f C",
                self._metrics_count,
                cpu,
                temp,
            )
