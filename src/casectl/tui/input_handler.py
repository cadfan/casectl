"""Keystroke input handling for the ``casectl top`` interactive dashboard.

Provides non-blocking terminal input reading and maps keystrokes to
commands dispatched to the daemon REST API:

- ``m`` — cycle fan mode (follow-temp -> follow-rpi -> manual -> off -> ...)
- ``+`` / ``=`` — increase fan speed by 10% (switches to manual mode)
- ``-`` — decrease fan speed by 10% (switches to manual mode)
- ``q`` — quit the dashboard

All commands are dispatched via HTTP PUT to the daemon API, matching the
same endpoints used by the CLI.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fan mode cycle order (excludes CUSTOM which is plugin-defined)
# ---------------------------------------------------------------------------

FAN_MODE_CYCLE: list[str] = ["follow-temp", "follow-rpi", "manual", "off"]

# Speed step in API percent (0-100 range)
SPEED_STEP: int = 10


# ---------------------------------------------------------------------------
# Non-blocking terminal input
# ---------------------------------------------------------------------------


def _is_real_terminal() -> bool:
    """Check if stdin is a real terminal (not a pipe or mock)."""
    try:
        return os.isatty(sys.stdin.fileno())
    except (AttributeError, ValueError, OSError):
        return False


def read_key_nonblocking() -> str | None:
    """Read a single keypress without blocking.

    Returns the character pressed, or ``None`` if no input is available
    or the terminal does not support raw mode.

    Uses ``select()`` on the raw stdin fd. The terminal is kept in raw
    mode for the entire ``casectl top`` session (set once in
    :func:`enter_raw_mode`) so that Rich's Live alternate screen and
    our keystroke reader don't fight over termios settings.
    """
    if not _is_real_terminal():
        return None

    try:
        import select
    except ImportError:
        return None

    fd = sys.stdin.fileno()
    try:
        rlist, _, _ = select.select([fd], [], [], 0)
        if rlist:
            ch = os.read(fd, 1).decode("utf-8", errors="ignore")
            return ch
        return None
    except (OSError, ValueError):
        return None


# Saved terminal settings for restore on exit.
_saved_termios: list[Any] | None = None


def enter_raw_mode() -> None:
    """Put the terminal into cbreak mode (like raw, but signals still work).

    Call once before the Live loop starts. Pair with :func:`exit_raw_mode`.
    """
    global _saved_termios  # noqa: PLW0603

    if not _is_real_terminal():
        return

    try:
        import termios
        import tty
    except ImportError:
        return

    fd = sys.stdin.fileno()
    try:
        _saved_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)  # cbreak = keys available immediately, signals still work
    except termios.error:
        _saved_termios = None


def exit_raw_mode() -> None:
    """Restore the terminal to its original settings."""
    global _saved_termios  # noqa: PLW0603

    if _saved_termios is None:
        return

    try:
        import termios
    except ImportError:
        return

    fd = sys.stdin.fileno()
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, _saved_termios)
    except termios.error:
        pass
    finally:
        _saved_termios = None


# ---------------------------------------------------------------------------
# API command dispatchers
# ---------------------------------------------------------------------------


def dispatch_fan_mode_cycle(
    base_url: str,
    current_mode: str | None,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Cycle to the next fan mode and send it to the daemon.

    Parameters
    ----------
    base_url:
        Daemon REST API base URL.
    current_mode:
        Current fan mode name (e.g. ``"follow_temp"``). If ``None`` or
        not in the cycle list, starts from the first mode.
    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    dict or None
        The API response dict on success, ``None`` on failure.
    """
    # Normalise the mode name (API returns underscore, we use hyphen)
    normalised = (current_mode or "").replace("_", "-").lower()

    try:
        idx = FAN_MODE_CYCLE.index(normalised)
        next_mode = FAN_MODE_CYCLE[(idx + 1) % len(FAN_MODE_CYCLE)]
    except ValueError:
        next_mode = FAN_MODE_CYCLE[0]

    try:
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            resp = client.put(
                "/api/plugins/fan-control/mode",
                json={"mode": next_mode},
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Fan mode cycle failed: %d %s", resp.status_code, resp.text)
    except Exception:
        logger.debug("Fan mode cycle request failed", exc_info=True)

    return None


def dispatch_fan_speed_change(
    base_url: str,
    current_duty: list[int] | None,
    delta: int = SPEED_STEP,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Increase or decrease fan speed and send it to the daemon.

    Adjusts duty by ``delta`` percentage points (positive to increase,
    negative to decrease). Clamps to 0-100 range.

    Parameters
    ----------
    base_url:
        Daemon REST API base URL.
    current_duty:
        Current per-channel duty values in hardware range (0-255).
        If ``None``, assumes 50% as baseline.
    delta:
        Percentage points to adjust (e.g. +10 or -10).
    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    dict or None
        The API response dict on success, ``None`` on failure.
    """
    if current_duty:
        # Convert from 0-255 hardware range to 0-100 API range
        avg_pct = int((sum(current_duty) / len(current_duty)) * 100 / 255)
    else:
        avg_pct = 50

    new_pct = max(0, min(100, avg_pct + delta))

    # Apply to all channels uniformly
    duty_list = [new_pct, new_pct, new_pct]

    try:
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            resp = client.put(
                "/api/plugins/fan-control/speed",
                json={"duty": duty_list},
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Fan speed change failed: %d %s", resp.status_code, resp.text)
    except Exception:
        logger.debug("Fan speed change request failed", exc_info=True)

    return None


# ---------------------------------------------------------------------------
# Key handler
# ---------------------------------------------------------------------------


class KeyHandler:
    """Maps keystrokes to dashboard commands.

    Attributes
    ----------
    base_url : str
        Daemon REST API base URL.
    last_action : str | None
        Description of the last action taken (for status display).
    quit_requested : bool
        Whether the user pressed ``q`` to quit.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.last_action: str | None = None
        self.quit_requested: bool = False

    def handle_key(self, key: str, dashboard_data: dict[str, Any]) -> None:
        """Process a single keypress.

        Parameters
        ----------
        key:
            The character that was pressed.
        dashboard_data:
            Current dashboard data dict (with keys ``fan``, ``led``, etc.)
            used to determine current state for cycling/adjustment.
        """
        if key == "q":
            self.quit_requested = True
            return

        if key == "m":
            self._handle_mode_cycle(dashboard_data)
        elif key in ("+", "="):
            self._handle_speed_change(dashboard_data, delta=SPEED_STEP)
        elif key == "-":
            self._handle_speed_change(dashboard_data, delta=-SPEED_STEP)

    def _handle_mode_cycle(self, dashboard_data: dict[str, Any]) -> None:
        """Cycle to the next fan mode."""
        fan_data = dashboard_data.get("fan")
        current_mode = fan_data.get("mode") if fan_data else None

        result = dispatch_fan_mode_cycle(self.base_url, current_mode)
        if result:
            new_mode = result.get("mode", "?")
            self.last_action = f"Fan mode -> {new_mode}"
        else:
            self.last_action = "Fan mode change failed"

    def _handle_speed_change(
        self,
        dashboard_data: dict[str, Any],
        delta: int,
    ) -> None:
        """Adjust fan speed up or down."""
        fan_data = dashboard_data.get("fan")
        current_duty = fan_data.get("duty") if fan_data else None

        result = dispatch_fan_speed_change(
            self.base_url,
            current_duty,
            delta=delta,
        )
        if result:
            direction = "+" if delta > 0 else ""
            self.last_action = f"Fan speed {direction}{delta}%"
        else:
            self.last_action = "Fan speed change failed"
