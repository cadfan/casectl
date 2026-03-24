"""Plugin protocol, context, and supporting types for the casectl plugin system.

Every casectl feature — fan control, LED patterns, OLED display, monitoring — is
a plugin that conforms to :class:`CasePlugin`.  The daemon creates a
:class:`PluginContext` for each plugin, giving it access to config, hardware,
events, and route registration without coupling plugins to daemon internals.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

import click
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plugin status
# ---------------------------------------------------------------------------

class PluginStatus(str, Enum):
    """Health status of a loaded plugin."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Hardware registry
# ---------------------------------------------------------------------------

class HardwareRegistry:
    """Thin container holding references to hardware abstraction objects.

    Plugins access hardware through this registry rather than importing drivers
    directly.  Any field may be ``None`` when the corresponding hardware is not
    present or its driver could not be loaded (e.g. running without I2C).
    """

    def __init__(
        self,
        expansion: Any | None = None,
        oled: Any | None = None,
        system_info: Any | None = None,
    ) -> None:
        self._expansion = expansion
        self._oled = oled
        self._system_info = system_info

    # -- typed aliases used in annotations elsewhere (kept generic for now) --
    # These will narrow to ExpansionBoard / OledDevice / SystemInfo once those
    # modules are implemented.

    @property
    def expansion(self) -> Any | None:
        """STM32 expansion board driver (I2C 0x21), or ``None``."""
        return self._expansion

    @property
    def oled(self) -> Any | None:
        """SSD1306 OLED display driver (I2C 0x3C), or ``None``."""
        return self._oled

    @property
    def system_info(self) -> Any | None:
        """System information provider (CPU temp, memory, etc.), or ``None``."""
        return self._system_info


# ---------------------------------------------------------------------------
# Plugin context — given to each plugin during setup()
# ---------------------------------------------------------------------------

class PluginContext:
    """Sandbox context provided to each plugin at setup time.

    The context is the *only* interface a plugin should use to interact with
    the rest of the system.  It provides:

    * Route registration (FastAPI ``APIRouter``)
    * Configuration schema registration and retrieval
    * CLI command group registration
    * Event bus subscription / emission
    * Hardware access
    * A per-plugin logger
    """

    def __init__(
        self,
        plugin_name: str,
        config_manager: Any,
        hardware_registry: HardwareRegistry,
        event_bus: Any,
    ) -> None:
        self._plugin_name = plugin_name
        self._config_manager = config_manager
        self._hardware_registry = hardware_registry
        self._event_bus = event_bus

        self._router: APIRouter | None = None
        self._config_schema: type[BaseModel] | None = None
        self._commands: click.Group | None = None
        self._logger = logging.getLogger(f"casectl.plugins.{plugin_name}")

    # -- registration helpers -----------------------------------------------

    def register_routes(self, router: APIRouter) -> None:
        """Register a FastAPI router to be mounted by the daemon.

        Parameters
        ----------
        router:
            A :class:`fastapi.APIRouter` whose routes will be served under
            ``/api/plugins/{plugin_name}``.
        """
        if self._router is not None:
            self._logger.warning(
                "Router already registered for plugin %r — overwriting",
                self._plugin_name,
            )
        self._router = router

    def register_config(self, schema: type[BaseModel]) -> None:
        """Register a Pydantic model that describes this plugin's config schema.

        Parameters
        ----------
        schema:
            A :class:`pydantic.BaseModel` subclass.  The daemon may use it to
            validate the ``plugins.<name>`` section of the config file.
        """
        if self._config_schema is not None:
            self._logger.warning(
                "Config schema already registered for plugin %r — overwriting",
                self._plugin_name,
            )
        self._config_schema = schema

    def register_commands(self, group: click.Group) -> None:
        """Register a Click command group to be attached to the CLI.

        Parameters
        ----------
        group:
            A :class:`click.Group` whose commands become sub-commands under
            ``casectl <plugin_name>``.
        """
        if self._commands is not None:
            self._logger.warning(
                "Commands already registered for plugin %r — overwriting",
                self._plugin_name,
            )
        self._commands = group

    # -- config access ------------------------------------------------------

    async def get_config(self) -> dict[str, Any]:
        """Return the ``plugins.<plugin_name>`` section from the config.

        Returns an empty dict if the section does not exist or the config
        manager is ``None``.
        """
        if self._config_manager is None:
            return {}

        # Support both sync and async config managers.
        try:
            config = self._config_manager.get(f"plugins.{self._plugin_name}")
        except (KeyError, AttributeError):
            return {}

        if config is None:
            return {}

        # If the config manager returns a Pydantic model, convert to dict.
        if isinstance(config, BaseModel):
            return config.model_dump()

        if isinstance(config, dict):
            return config

        return {}

    # -- hardware -----------------------------------------------------------

    def get_hardware(self) -> HardwareRegistry:
        """Return the shared :class:`HardwareRegistry`."""
        return self._hardware_registry

    # -- events -------------------------------------------------------------

    def on_event(self, event: str, handler: Callable[..., Any]) -> None:
        """Subscribe *handler* to *event* on the event bus.

        Parameters
        ----------
        event:
            The event name (e.g. ``"temperature.changed"``).
        handler:
            An async or sync callable ``(data) -> None``.
        """
        if self._event_bus is None:
            self._logger.warning(
                "No event bus available — cannot subscribe to %r", event,
            )
            return
        self._event_bus.subscribe(event, handler)

    def emit_event(self, event: str, data: Any = None) -> None:
        """Emit *event* with optional *data* on the event bus.

        Note: emission is fire-and-forget from the plugin's perspective.
        Handlers are invoked asynchronously by the event bus.
        """
        if self._event_bus is None:
            self._logger.warning(
                "No event bus available — cannot emit %r", event,
            )
            return
        # The event bus exposes an async ``emit`` — the daemon's event loop
        # will schedule it.  We store a coroutine reference; callers that need
        # to await should use the event bus directly.
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._event_bus.emit(event, data))
        except RuntimeError:
            # No running loop — log a warning.  This can happen during tests
            # or when called from a synchronous context.
            self._logger.warning(
                "emit_event(%r) called outside an async context — event dropped",
                event,
            )

    # -- properties ---------------------------------------------------------

    @property
    def logger(self) -> logging.Logger:
        """Per-plugin logger, named ``casectl.plugins.<name>``."""
        return self._logger

    @property
    def routes(self) -> APIRouter | None:
        """The FastAPI router registered by this plugin, or ``None``."""
        return self._router

    @property
    def config_schema(self) -> type[BaseModel] | None:
        """The Pydantic config schema registered by this plugin, or ``None``."""
        return self._config_schema

    @property
    def commands(self) -> click.Group | None:
        """The Click command group registered by this plugin, or ``None``."""
        return self._commands


# ---------------------------------------------------------------------------
# CasePlugin protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class CasePlugin(Protocol):
    """Protocol that every casectl plugin must satisfy.

    Plugins are discovered either as built-in modules under
    ``casectl.plugins.*`` or via the ``casectl.plugins`` entry-point group.

    Lifecycle:

    1. **Instantiation** — the plugin host creates an instance.
    2. **setup(ctx)** — the plugin receives its :class:`PluginContext` and
       should register routes, config schemas, and CLI commands.
    3. **start()** — the daemon is ready; the plugin may begin background work.
    4. **stop()** — the daemon is shutting down; the plugin must clean up.

    Attributes on the class/instance:

    * ``name`` — unique short name (e.g. ``"fan-control"``).
    * ``version`` — SemVer string (e.g. ``"0.1.0"``).
    * ``description`` — one-line human-readable description.
    * ``min_daemon_version`` — minimum casectl daemon version required.
    """

    name: str
    version: str
    description: str
    min_daemon_version: str

    async def setup(self, ctx: PluginContext) -> None:
        """Receive context and register routes / config / commands."""
        ...

    async def start(self) -> None:
        """Start background tasks.  Called after all plugins are set up."""
        ...

    async def stop(self) -> None:
        """Stop background tasks and release resources."""
        ...

    def get_status(self) -> dict[str, Any]:
        """Return plugin health and diagnostic information.

        The dict should include at least ``{"status": PluginStatus}``.
        Additional keys are plugin-specific.
        """
        ...
