"""Plugin lifecycle manager for the casectl daemon.

The :class:`PluginHost` discovers, loads, wires, and manages plugins through
their full lifecycle: **load -> setup -> start -> stop**.

Discovery sources:

1. **Built-in plugins** — imported from ``casectl.plugins.<name>``.
2. **Community plugins** — discovered via the ``casectl.plugins`` entry-point
   group (defined in third-party ``pyproject.toml`` files).
"""

from __future__ import annotations

import importlib
import logging
import traceback
from typing import Any

from fastapi import APIRouter

from casectl.plugins.base import CasePlugin, HardwareRegistry, PluginContext, PluginStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in plugin registry
# ---------------------------------------------------------------------------

# (module_path, class_name) for each built-in plugin.
_BUILTIN_PLUGINS: list[tuple[str, str]] = [
    ("casectl.plugins.fan", "FanControlPlugin"),
    ("casectl.plugins.led", "LedControlPlugin"),
    ("casectl.plugins.oled", "OledDisplayPlugin"),
    ("casectl.plugins.monitor", "SystemMonitorPlugin"),
    ("casectl.plugins.prometheus", "PrometheusPlugin"),
]


# ---------------------------------------------------------------------------
# Version comparison helper
# ---------------------------------------------------------------------------

def _version_satisfies(current: str, minimum: str) -> bool:
    """Return ``True`` if *current* >= *minimum* using SemVer ordering.

    Uses :mod:`packaging.version` when available, otherwise falls back to a
    naive tuple comparison of dot-separated integers.
    """
    try:
        from packaging.version import Version

        return Version(current) >= Version(minimum)
    except ImportError:
        pass

    # Fallback: split on "." and compare integer tuples.
    def _to_tuple(v: str) -> tuple[int, ...]:
        parts: list[int] = []
        for segment in v.strip().split("."):
            # Strip pre-release suffixes like "0.1.0a1" → "0"
            num = ""
            for ch in segment:
                if ch.isdigit():
                    num += ch
                else:
                    break
            parts.append(int(num) if num else 0)
        return tuple(parts)

    try:
        return _to_tuple(current) >= _to_tuple(minimum)
    except (ValueError, TypeError):
        logger.warning(
            "Could not compare versions %r and %r — allowing plugin to load",
            current,
            minimum,
        )
        return True


# ---------------------------------------------------------------------------
# PluginHost
# ---------------------------------------------------------------------------

class PluginHost:
    """Discovers, loads, and manages the lifecycle of all casectl plugins.

    Parameters
    ----------
    config_manager:
        The application's config manager (provides ``get(key)``).
    hardware_registry:
        Shared :class:`HardwareRegistry` instance.
    event_bus:
        Shared :class:`EventBus` instance.
    daemon_version:
        The running daemon's version string, used for compatibility checks
        against each plugin's ``min_daemon_version``.
    """

    def __init__(
        self,
        config_manager: Any,
        hardware_registry: HardwareRegistry,
        event_bus: Any,
        daemon_version: str = "0.1.0",
    ) -> None:
        self._config_manager = config_manager
        self._hardware_registry = hardware_registry
        self._event_bus = event_bus
        self._daemon_version = daemon_version

        self._plugins: dict[str, CasePlugin] = {}
        self._contexts: dict[str, PluginContext] = {}
        self._routes: dict[str, APIRouter] = {}  # prefix -> router
        self._plugin_statuses: dict[str, PluginStatus] = {}

    # ------------------------------------------------------------------
    # Discovery & loading
    # ------------------------------------------------------------------

    async def load_plugins(self) -> None:
        """Discover and set up all built-in and community plugins.

        For each discovered plugin class:

        1. Check ``min_daemon_version`` against the running daemon.
        2. Instantiate the class.
        3. Create a :class:`PluginContext` and call ``setup(ctx)``.
        4. Verify that any registered routes do not collide with existing ones.
        5. Store the plugin, context, and routes.

        Errors at any step are logged and the plugin is skipped (or marked
        ERROR) — one bad plugin never takes down the daemon.
        """
        classes = self._discover_plugin_classes()

        for plugin_cls in classes:
            await self._load_single_plugin(plugin_cls)

        loaded = list(self._plugins.keys())
        errored = [
            name for name, status in self._plugin_statuses.items()
            if status == PluginStatus.ERROR
        ]
        logger.info(
            "Plugin loading complete: %d loaded, %d errored",
            len(loaded),
            len(errored),
        )

    def _discover_plugin_classes(self) -> list[type]:
        """Return a list of plugin classes from built-in modules and entry points."""
        classes: list[type] = []

        # -- Built-in plugins -----------------------------------------------
        for module_path, class_name in _BUILTIN_PLUGINS:
            try:
                module = importlib.import_module(module_path)
                cls = getattr(module, class_name, None)
                if cls is None:
                    logger.debug(
                        "Built-in plugin class %s.%s not found — skipping "
                        "(module exists but class not yet implemented)",
                        module_path,
                        class_name,
                    )
                    continue
                classes.append(cls)
                logger.debug("Discovered built-in plugin: %s.%s", module_path, class_name)
            except ImportError:
                logger.warning(
                    "Failed to import built-in plugin module %r:\n%s",
                    module_path,
                    traceback.format_exc(),
                )
            except Exception:
                logger.warning(
                    "Unexpected error importing built-in plugin %r:\n%s",
                    module_path,
                    traceback.format_exc(),
                )

        # -- Community plugins via entry points -----------------------------
        try:
            from importlib.metadata import entry_points

            eps = entry_points(group="casectl.plugins")
            for ep in eps:
                # Skip entry points that match built-in module paths — those
                # were already handled above.
                builtin_names = {cls_name for _, cls_name in _BUILTIN_PLUGINS}
                if ep.value.split(":")[-1] in builtin_names:
                    # This is a built-in registered as an entry point; we
                    # already imported it above.
                    continue

                try:
                    cls = ep.load()
                    classes.append(cls)
                    logger.debug("Discovered community plugin: %s (%s)", ep.name, ep.value)
                except ImportError:
                    logger.warning(
                        "Failed to import community plugin %r (%s):\n%s",
                        ep.name,
                        ep.value,
                        traceback.format_exc(),
                    )
                except Exception:
                    logger.warning(
                        "Unexpected error loading community plugin %r:\n%s",
                        ep.name,
                        traceback.format_exc(),
                    )
        except Exception:
            logger.warning(
                "Failed to enumerate entry points for group 'casectl.plugins':\n%s",
                traceback.format_exc(),
            )

        return classes

    async def _load_single_plugin(self, plugin_cls: type) -> None:
        """Instantiate, version-check, set up, and register a single plugin."""
        # -- Instantiate ----------------------------------------------------
        try:
            plugin = plugin_cls()
        except Exception:
            logger.error(
                "Failed to instantiate plugin class %r:\n%s",
                plugin_cls,
                traceback.format_exc(),
            )
            return

        name: str = getattr(plugin, "name", None) or plugin_cls.__name__
        version: str = getattr(plugin, "version", "0.0.0")
        min_daemon: str = getattr(plugin, "min_daemon_version", "0.0.0")

        # -- Duplicate name check -------------------------------------------
        if name in self._plugins:
            logger.warning(
                "Plugin %r already loaded — skipping duplicate from %r",
                name,
                plugin_cls,
            )
            return

        # -- Version compatibility ------------------------------------------
        if not _version_satisfies(self._daemon_version, min_daemon):
            logger.warning(
                "Plugin %r v%s requires daemon >= %s (running %s) — skipping",
                name,
                version,
                min_daemon,
                self._daemon_version,
            )
            return

        # -- Create context and call setup() --------------------------------
        ctx = PluginContext(
            plugin_name=name,
            config_manager=self._config_manager,
            hardware_registry=self._hardware_registry,
            event_bus=self._event_bus,
        )

        try:
            await plugin.setup(ctx)
        except Exception:
            logger.error(
                "Plugin %r raised an exception during setup:\n%s",
                name,
                traceback.format_exc(),
            )
            self._plugins[name] = plugin
            self._contexts[name] = ctx
            self._plugin_statuses[name] = PluginStatus.ERROR
            return

        # -- Route deduplication --------------------------------------------
        if ctx.routes is not None:
            prefix = f"/api/plugins/{name}"
            if prefix in self._routes:
                logger.warning(
                    "Route prefix %r already registered — skipping plugin %r",
                    prefix,
                    name,
                )
                return
            self._routes[prefix] = ctx.routes

        # -- Store ----------------------------------------------------------
        self._plugins[name] = plugin
        self._contexts[name] = ctx
        self._plugin_statuses[name] = PluginStatus.STOPPED

        logger.info("Loaded plugin %r v%s", name, version)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """Call ``start()`` on every loaded plugin.

        Exceptions are caught per-plugin so that one failing plugin does not
        prevent others from starting.
        """
        for name, plugin in self._plugins.items():
            if self._plugin_statuses.get(name) == PluginStatus.ERROR:
                logger.debug("Skipping start for errored plugin %r", name)
                continue

            try:
                await plugin.start()
                self._plugin_statuses[name] = PluginStatus.HEALTHY
                logger.info("Started plugin %r", name)
            except Exception:
                logger.error(
                    "Plugin %r failed to start:\n%s",
                    name,
                    traceback.format_exc(),
                )
                self._plugin_statuses[name] = PluginStatus.ERROR

    async def stop_all(self) -> None:
        """Call ``stop()`` on every loaded plugin in reverse load order.

        Exceptions are caught per-plugin.
        """
        for name in reversed(list(self._plugins.keys())):
            plugin = self._plugins[name]
            try:
                await plugin.stop()
                self._plugin_statuses[name] = PluginStatus.STOPPED
                logger.info("Stopped plugin %r", name)
            except Exception:
                logger.error(
                    "Plugin %r failed to stop cleanly:\n%s",
                    name,
                    traceback.format_exc(),
                )
                self._plugin_statuses[name] = PluginStatus.ERROR

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_plugin(self, name: str) -> CasePlugin | None:
        """Return the plugin instance with the given *name*, or ``None``."""
        return self._plugins.get(name)

    def get_all_statuses(self) -> dict[str, PluginStatus]:
        """Return a mapping of plugin name to current :class:`PluginStatus`.

        For plugins that report their own status via ``get_status()``, the
        ``"status"`` key in the returned dict takes precedence.
        """
        statuses: dict[str, PluginStatus] = {}
        for name, plugin in self._plugins.items():
            # Prefer the plugin's self-reported status when available.
            try:
                info = plugin.get_status()
                raw_status = info.get("status") if isinstance(info, dict) else None
                if isinstance(raw_status, PluginStatus):
                    statuses[name] = raw_status
                elif isinstance(raw_status, str):
                    statuses[name] = PluginStatus(raw_status)
                else:
                    statuses[name] = self._plugin_statuses.get(name, PluginStatus.STOPPED)
            except Exception:
                statuses[name] = self._plugin_statuses.get(name, PluginStatus.ERROR)
        return statuses

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return summary information for every loaded plugin.

        Each dict contains ``name``, ``version``, ``status``, and
        ``description``.
        """
        all_statuses = self.get_all_statuses()
        result: list[dict[str, Any]] = []
        for name, plugin in self._plugins.items():
            result.append({
                "name": name,
                "version": getattr(plugin, "version", "unknown"),
                "status": all_statuses.get(name, PluginStatus.STOPPED).value,
                "description": getattr(plugin, "description", ""),
            })
        return result

    def get_routes(self) -> list[tuple[str, APIRouter]]:
        """Return ``(prefix, router)`` pairs for mounting into FastAPI.

        Example usage::

            for prefix, router in plugin_host.get_routes():
                app.include_router(router, prefix=prefix)
        """
        return list(self._routes.items())
