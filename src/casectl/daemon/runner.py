"""Main daemon runner — wires all components together and starts the server.

:func:`run_daemon` is the single entry point that:

1. Loads configuration via :class:`~casectl.config.manager.ConfigManager`.
2. Probes and initialises hardware (expansion board, OLED, system info).
3. Creates the :class:`~casectl.daemon.event_bus.EventBus`.
4. Creates the :class:`~casectl.daemon.plugin_host.PluginHost`, discovers
   and loads plugins.
5. Builds the FastAPI application via :func:`~casectl.daemon.server.create_app`.
6. Runs the Uvicorn ASGI server with graceful signal handling.

All hardware and import failures degrade gracefully — casectl can run on a
plain Linux host with no I2C devices attached.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from casectl.daemon.plugin_host import PluginHost
    from casectl.hardware.expansion import ExpansionBoard
    from casectl.hardware.oled import OledDevice

logger = logging.getLogger(__name__)

_LOCK_PATH = Path.home() / ".config" / "casectl" / "daemon.pid"


def _acquire_lock() -> IO[str]:
    """Acquire exclusive PID lock. Raises SystemExit if already held."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(_LOCK_PATH, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise SystemExit("Another casectl daemon is already running.")
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

try:
    from casectl import __version__ as _DAEMON_VERSION
except ImportError:
    _DAEMON_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Hardware initialisation helpers
# ---------------------------------------------------------------------------


def _init_expansion_board() -> ExpansionBoard | None:
    """Probe for and initialise the STM32 expansion board.

    Returns ``None`` if the hardware is not present, the smbus2 library is
    missing, or initialisation fails.  All errors are logged and swallowed.
    """
    try:
        from casectl.hardware.detect import is_case_hardware_present
    except ImportError:
        logger.debug("Hardware detection module not available — skipping expansion board")
        return None

    if not is_case_hardware_present():
        logger.info("Expansion board not detected on I2C — running without it")
        return None

    try:
        from casectl.hardware.expansion import ExpansionBoard, FanHwMode

        expansion = ExpansionBoard()
        if not expansion.connected:
            logger.warning("Expansion board instantiated but not connected — ignoring")
            return None

        # Put the STM32 fans into MANUAL mode so that casectl controls duty
        # directly rather than the STM32's own auto-temperature algorithm.
        try:
            expansion.set_fan_mode(FanHwMode.MANUAL)
            logger.info("Expansion board initialised; fan mode set to MANUAL")
        except OSError:
            logger.warning(
                "Could not set fan mode to MANUAL — fans may run at STM32 defaults",
                exc_info=True,
            )

        return expansion

    except ImportError:
        logger.debug("casectl.hardware.expansion not importable — no expansion board support")
        return None
    except Exception:
        logger.warning("Unexpected error initialising expansion board", exc_info=True)
        return None


def _init_oled(rotation_degrees: int) -> OledDevice | None:
    """Probe for and initialise the SSD1306 OLED display.

    Parameters
    ----------
    rotation_degrees:
        Display rotation from config — one of ``0``, ``90``, ``180``, ``270``.
        Converted to a luma rotation index (0-3).

    Returns ``None`` if the display is not detected or initialisation fails.
    """
    try:
        from casectl.hardware.detect import is_oled_present
    except ImportError:
        logger.debug("Hardware detection module not available — skipping OLED")
        return None

    if not is_oled_present():
        logger.info("OLED display not detected on I2C — running without it")
        return None

    try:
        from casectl.hardware.oled import OledDevice

        # Map degrees to luma rotation index: 0°→0, 90°→1, 180°→2, 270°→3
        rotation_map: dict[int, int] = {0: 0, 90: 1, 180: 2, 270: 3}
        rotate: int = rotation_map.get(rotation_degrees, 0)
        if rotation_degrees not in rotation_map:
            logger.warning(
                "Invalid OLED rotation %d° in config — defaulting to 0°",
                rotation_degrees,
            )

        oled = OledDevice(rotation=rotate)
        if not oled.available:
            logger.warning("OLED device created but reports unavailable — ignoring")
            return None

        logger.info("OLED display initialised (rotation=%d°)", rotation_degrees)
        return oled

    except ImportError:
        logger.debug("casectl.hardware.oled not importable — no OLED support")
        return None
    except Exception:
        logger.warning("Unexpected error initialising OLED display", exc_info=True)
        return None


def _init_system_info() -> object | None:
    """Create a :class:`~casectl.hardware.system.SystemInfo` instance.

    Returns ``None`` only if the module cannot be imported (e.g. psutil
    is not installed).
    """
    try:
        from casectl.hardware.system import SystemInfo

        system_info = SystemInfo()
        logger.debug("SystemInfo provider created")
        return system_info
    except ImportError:
        logger.warning("casectl.hardware.system not importable (psutil missing?) — no system metrics")
        return None
    except Exception:
        logger.warning("Unexpected error creating SystemInfo", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


async def _shutdown(
    server: uvicorn.Server,
    plugin_host: PluginHost,
    expansion: ExpansionBoard | None,
    oled: OledDevice | None,
    lock_file: IO[str] | None = None,
) -> None:
    """Perform an orderly shutdown of all daemon components.

    Called when SIGTERM or SIGINT is received.  Each step is individually
    wrapped in a try/except so that one failure does not prevent the others
    from cleaning up.

    Parameters
    ----------
    server:
        The Uvicorn server instance — ``should_exit`` is set to make it
        stop its event loop.
    plugin_host:
        The plugin host — ``stop_all()`` is awaited to give plugins a
        chance to release resources.
    expansion:
        The expansion board driver, or ``None``.  Closed synchronously.
    oled:
        The OLED device, or ``None``.  Closed synchronously.
    """
    logger.info("Shutting down casectl daemon...")

    # 1. Stop all plugins
    try:
        await plugin_host.stop_all()
    except Exception:
        logger.error("Error stopping plugins during shutdown", exc_info=True)

    # 2. Close hardware handles
    if expansion is not None:
        try:
            expansion.close()
            logger.debug("Expansion board closed")
        except Exception:
            logger.error("Error closing expansion board", exc_info=True)

    if oled is not None:
        try:
            oled.close()
            logger.debug("OLED device closed")
        except Exception:
            logger.error("Error closing OLED device", exc_info=True)

    # 3. Release PID lock
    if lock_file is not None:
        try:
            lock_file.close()
            _LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    # 4. Tell Uvicorn to exit
    server.should_exit = True
    logger.info("casectl daemon shutdown complete")


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


def _configure_logging(level: int = logging.INFO) -> None:
    """Set up structured logging for the daemon process.

    Parameters
    ----------
    level:
        Minimum log level.  Defaults to ``INFO``.
    """
    root_logger = logging.getLogger()
    # Avoid adding duplicate handlers if run_daemon is called multiple times
    # (e.g. in tests).
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root_logger.addHandler(handler)

    root_logger.setLevel(level)

    # Quieten noisy third-party loggers
    for noisy in ("uvicorn.access", "uvicorn.error", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_daemon(
    host: str | None = None,
    port: int | None = None,
    log_level: str = "info",
) -> None:
    """Main daemon entry point.  Wires up all components and runs the server.

    This coroutine is expected to be the top-level ``asyncio.run()`` target.
    It does not return until the server is shut down via signal.

    Parameters
    ----------
    host:
        Bind address override.  When ``None``, the value from
        ``config.service.api_host`` is used (default ``127.0.0.1``).
    port:
        Bind port override.  When ``None``, the value from
        ``config.service.api_port`` is used (default ``8420``).
    log_level:
        Logging level name (``"debug"``, ``"info"``, ``"warning"``, etc.).
    """
    # -- 0. PID lock --------------------------------------------------------
    lock_file = _acquire_lock()

    # -- 0a. Logging --------------------------------------------------------
    numeric_level: int = getattr(logging, log_level.upper(), logging.INFO)
    _configure_logging(level=numeric_level)

    logger.info("casectl v%s daemon initialising", _DAEMON_VERSION)

    # -- 1. Configuration --------------------------------------------------
    from casectl.config.manager import ConfigManager

    config_manager = ConfigManager()
    try:
        config = await config_manager.load()
        logger.info("Configuration loaded from %s", config_manager.path)
    except Exception:
        logger.error(
            "Failed to load configuration — using defaults", exc_info=True
        )
        from casectl.config.models import CaseCtlConfig

        config = CaseCtlConfig()

    # -- 2. Hardware --------------------------------------------------------
    expansion = _init_expansion_board()
    oled = _init_oled(rotation_degrees=config.oled.rotation)
    system_info = _init_system_info()

    from casectl.plugins.base import HardwareRegistry

    hardware = HardwareRegistry(
        expansion=expansion,
        oled=oled,
        system_info=system_info,
    )

    logger.info(
        "Hardware: expansion=%s, oled=%s, system_info=%s",
        "connected" if expansion is not None else "absent",
        "available" if oled is not None else "absent",
        "ready" if system_info is not None else "absent",
    )

    # -- 3. Event bus -------------------------------------------------------
    from casectl.daemon.event_bus import EventBus

    event_bus = EventBus()

    # -- 4. Plugin host -----------------------------------------------------
    from casectl.daemon.plugin_host import PluginHost

    plugin_host = PluginHost(
        config_manager=config_manager,
        hardware_registry=hardware,
        event_bus=event_bus,
        daemon_version=_DAEMON_VERSION,
    )
    await plugin_host.load_plugins()

    # NOTE: plugin_host.start_all() is called by the FastAPI lifespan hook
    # inside create_app() so that plugins start *after* the ASGI server is
    # ready to accept connections.  We do NOT call it here to avoid double-
    # starting.

    # -- 5. FastAPI application ---------------------------------------------
    from casectl.daemon.server import create_app

    # -- 6. Resolve bind address/port --------------------------------------
    actual_host: str = host if host is not None else config.service.api_host
    actual_port: int = port if port is not None else config.service.api_port

    app = create_app(plugin_host, config_manager, event_bus, host=actual_host,
                      port=actual_port, trust_proxy=config.service.trust_proxy)

    # -- 7. Uvicorn server --------------------------------------------------
    uvi_config = uvicorn.Config(
        app,
        host=actual_host,
        port=actual_port,
        log_level=log_level.lower(),
        # Disable uvicorn's own signal handling — we install our own below.
        # This is necessary because uvicorn.Server.serve() installs handlers
        # that call sys.exit(), which bypasses our cleanup logic.
        # Setting install_signal_handlers=False ensures our _shutdown() runs.
    )
    server = uvicorn.Server(uvi_config)

    # Disable uvicorn's default signal handling so our handler runs instead.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    # -- 8. Signal handlers -------------------------------------------------
    loop = asyncio.get_running_loop()
    shutdown_triggered: bool = False

    def _on_signal() -> None:
        nonlocal shutdown_triggered
        if shutdown_triggered:
            # Second signal — force exit.
            logger.warning("Received second signal — forcing exit")
            sys.exit(1)
        shutdown_triggered = True
        asyncio.ensure_future(
            _shutdown(server, plugin_host, expansion, oled, lock_file),
            loop=loop,
        )

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    # -- 9. Serve -----------------------------------------------------------
    logger.info(
        "casectl daemon starting on %s:%d", actual_host, actual_port
    )

    try:
        await server.serve()
    except Exception:
        logger.error("Uvicorn server exited with an error", exc_info=True)
    finally:
        # Ensure cleanup even if serve() raises without a signal.
        if not shutdown_triggered:
            await _shutdown(server, plugin_host, expansion, oled, lock_file)

    logger.info("casectl daemon exited")
