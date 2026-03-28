"""Interactive ``casectl top`` terminal dashboard using Rich Live.

Displays real-time system stats in a multi-panel layout:
- CPU temperature, usage, memory, disk
- Fan speed/mode per channel
- RGB LED status (mode + colour swatch)
- System info (IP, uptime, date/time)

Data is fetched from the daemon REST API at a configurable refresh interval.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import timedelta
from typing import Any

import httpx
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_REFRESH_INTERVAL: float = 2.0
MIN_REFRESH_INTERVAL: float = 0.5
MAX_REFRESH_INTERVAL: float = 60.0


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def fetch_dashboard_data(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """Fetch all dashboard data from the daemon API in one pass.

    Returns a dict with keys ``health``, ``monitor``, ``fan``, ``led``.
    Each value is either the parsed JSON response or ``None`` on failure.
    """
    result: dict[str, Any] = {
        "health": None,
        "monitor": None,
        "fan": None,
        "led": None,
    }

    try:
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            # Health endpoint
            try:
                resp = client.get("/api/health")
                if resp.status_code == 200:
                    result["health"] = resp.json()
            except Exception:
                pass

            # Monitor metrics
            try:
                resp = client.get("/api/plugins/system-monitor/status")
                if resp.status_code == 200:
                    result["monitor"] = resp.json()
            except Exception:
                pass

            # Fan status
            try:
                resp = client.get("/api/plugins/fan-control/status")
                if resp.status_code == 200:
                    result["fan"] = resp.json()
            except Exception:
                pass

            # LED status
            try:
                resp = client.get("/api/plugins/led-control/status")
                if resp.status_code == 200:
                    result["led"] = resp.json()
            except Exception:
                pass

    except httpx.ConnectError:
        pass  # daemon not reachable — all values stay None

    return result


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def _temp_bar(temp: float, max_temp: float = 100.0, width: int = 20) -> Text:
    """Build a coloured temperature bar."""
    ratio = min(max(temp / max_temp, 0.0), 1.0)
    filled = int(ratio * width)
    empty = width - filled

    if temp < 50:
        colour = "green"
    elif temp < 70:
        colour = "yellow"
    else:
        colour = "red"

    bar = Text()
    bar.append("\u2588" * filled, style=colour)
    bar.append("\u2591" * empty, style="dim")
    bar.append(f" {temp:.1f}\u00b0C", style=f"bold {colour}")
    return bar


def _percent_bar(pct: float, width: int = 20) -> Text:
    """Build a coloured percentage bar."""
    ratio = min(max(pct / 100.0, 0.0), 1.0)
    filled = int(ratio * width)
    empty = width - filled

    if pct < 60:
        colour = "green"
    elif pct < 85:
        colour = "yellow"
    else:
        colour = "red"

    bar = Text()
    bar.append("\u2588" * filled, style=colour)
    bar.append("\u2591" * empty, style="dim")
    bar.append(f" {pct:.1f}%", style=f"bold {colour}")
    return bar


def build_cpu_panel(data: dict[str, Any] | None) -> Panel:
    """Build the CPU / System metrics panel."""
    table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
    table.add_column("Label", style="bold", ratio=1)
    table.add_column("Value", ratio=3)

    if data is None:
        table.add_row("Status", Text("No data", style="dim red"))
    else:
        metrics = data.get("metrics", data)
        cpu_temp = metrics.get("cpu_temp", 0.0)
        cpu_pct = metrics.get("cpu_percent", 0.0)
        mem_pct = metrics.get("memory_percent", 0.0)
        disk_pct = metrics.get("disk_percent", 0.0)
        case_temp = metrics.get("case_temp", 0.0)

        table.add_row("CPU Temp", _temp_bar(cpu_temp))
        table.add_row("Case Temp", _temp_bar(case_temp))
        table.add_row("CPU", _percent_bar(cpu_pct))
        table.add_row("Memory", _percent_bar(mem_pct))
        table.add_row("Disk", _percent_bar(disk_pct))

    return Panel(table, title="[bold cyan]System Metrics[/]", border_style="cyan")


def build_fan_panel(data: dict[str, Any] | None) -> Panel:
    """Build the fan status panel."""
    table = Table(show_header=True, expand=True, box=None, padding=(0, 1))
    table.add_column("Channel", style="bold", justify="center")
    table.add_column("Duty", justify="center")
    table.add_column("RPM", justify="center")

    if data is None:
        table.add_row("--", Text("No data", style="dim red"), "--")
    else:
        mode = data.get("mode", "unknown")
        degraded = data.get("degraded", False)
        duty = data.get("duty", [])
        rpm = data.get("rpm", [])

        for i in range(max(len(duty), len(rpm), 1)):
            d = duty[i] if i < len(duty) else 0
            r = rpm[i] if i < len(rpm) else 0

            # Duty as a percentage-like bar (0-255 → 0-100%)
            duty_pct = (d / 255.0) * 100.0 if d > 0 else 0.0
            duty_text = Text(f"{d} ({duty_pct:.0f}%)")
            if duty_pct > 80:
                duty_text.stylize("bold red")
            elif duty_pct > 40:
                duty_text.stylize("yellow")
            else:
                duty_text.stylize("green")

            table.add_row(f"Fan {i}", duty_text, str(r))

    # Build subtitle with mode info
    mode_str = ""
    if data is not None:
        mode = data.get("mode", "unknown")
        degraded = data.get("degraded", False)
        health = "[red]DEGRADED[/]" if degraded else "[green]OK[/]"
        mode_str = f"  mode={mode}  {health}"

    return Panel(
        table,
        title="[bold cyan]Fan Status[/]",
        subtitle=mode_str if mode_str else None,
        border_style="cyan",
    )


def build_led_panel(data: dict[str, Any] | None) -> Panel:
    """Build the LED status panel."""
    table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
    table.add_column("Label", style="bold", ratio=1)
    table.add_column("Value", ratio=3)

    if data is None:
        table.add_row("Status", Text("No data", style="dim red"))
    else:
        mode = data.get("mode", "unknown")
        color = data.get("color", {})
        r = color.get("red", 0)
        g = color.get("green", 0)
        b = color.get("blue", 0)
        degraded = data.get("degraded", False)

        # Mode
        mode_text = Text(mode.upper())
        if mode == "off":
            mode_text.stylize("dim")
        elif mode == "rainbow":
            mode_text.stylize("bold magenta")
        elif mode == "breathing":
            mode_text.stylize("bold blue")
        else:
            mode_text.stylize("bold green")

        table.add_row("Mode", mode_text)

        # Colour swatch
        hex_code = f"#{r:02X}{g:02X}{b:02X}"
        swatch = Text(f"  {hex_code}  ")
        swatch.stylize(f"on rgb({r},{g},{b})")
        table.add_row("Colour", swatch)
        table.add_row("RGB", Text(f"({r}, {g}, {b})"))

        # Health
        health_text = Text("DEGRADED", style="bold red") if degraded else Text("OK", style="green")
        table.add_row("Health", health_text)

    return Panel(table, title="[bold cyan]LED Status[/]", border_style="cyan")


def build_info_panel(
    health: dict[str, Any] | None,
    monitor: dict[str, Any] | None,
    refresh_interval: float,
) -> Panel:
    """Build the system info panel (uptime, IP, plugins)."""
    table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
    table.add_column("Label", style="bold", ratio=1)
    table.add_column("Value", ratio=3)

    if health is not None:
        version = health.get("version", "?")
        status = health.get("status", "unknown")
        uptime = health.get("uptime", 0)

        status_text = Text(status.upper())
        if status == "healthy":
            status_text.stylize("bold green")
        elif status == "degraded":
            status_text.stylize("bold yellow")
        else:
            status_text.stylize("bold red")

        table.add_row("Daemon", status_text)
        table.add_row("Version", Text(str(version)))
        table.add_row("Uptime", Text(str(timedelta(seconds=int(uptime)))))

        plugins = health.get("plugins", [])
        for p in plugins:
            name = p.get("name", "?")
            p_status = p.get("status", "unknown")
            colour = "green" if p_status == "running" else "red"
            table.add_row(f"  {name}", Text(p_status, style=colour))
    else:
        table.add_row("Daemon", Text("NOT CONNECTED", style="bold red"))

    if monitor is not None:
        metrics = monitor.get("metrics", monitor)
        ip = metrics.get("ip_address", "")
        if ip:
            table.add_row("IP", Text(ip))
        date_str = metrics.get("date", "")
        time_str = metrics.get("time", "")
        if date_str or time_str:
            table.add_row("Clock", Text(f"{date_str} {time_str}".strip()))

    table.add_row("Refresh", Text(f"{refresh_interval:.1f}s"))

    return Panel(table, title="[bold cyan]Info[/]", border_style="cyan")


def build_header(
    connected: bool,
    last_action: str | None = None,
) -> Panel:
    """Build the top header bar with keybinding hints.

    Parameters
    ----------
    connected:
        Whether the daemon is reachable.
    last_action:
        Description of the last user action (shown briefly as feedback).
    """
    if connected:
        status = Text(" CONNECTED ", style="bold white on green")
    else:
        status = Text(" DISCONNECTED ", style="bold white on red")

    title = Text()
    title.append("casectl top", style="bold cyan")
    title.append("  ")
    title.append(status)
    title.append("  ")

    # Keybinding hints
    title.append("m", style="bold yellow")
    title.append("=mode ", style="dim")
    title.append("+/-", style="bold yellow")
    title.append("=speed ", style="dim")
    title.append("q", style="bold yellow")
    title.append("=quit", style="dim")

    # Show last action feedback
    if last_action:
        title.append("  ")
        title.append(f"[{last_action}]", style="bold green")

    return Panel(title, style="bold", height=3)


# ---------------------------------------------------------------------------
# Layout assembly
# ---------------------------------------------------------------------------


def build_layout(
    data: dict[str, Any],
    refresh_interval: float,
    last_action: str | None = None,
) -> Layout:
    """Assemble the full dashboard layout from fetched data.

    Parameters
    ----------
    data:
        Dict with keys ``health``, ``monitor``, ``fan``, ``led`` — each
        either parsed JSON or ``None``.
    refresh_interval:
        Current refresh interval in seconds (displayed in the info panel).
    last_action:
        Description of the last user action (shown in header as feedback).

    Returns
    -------
    Layout
        A Rich Layout ready to be rendered by ``Live``.
    """
    connected = data.get("health") is not None

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )

    layout["header"].update(build_header(connected, last_action=last_action))

    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=2),
    )

    layout["left"].split_column(
        Layout(name="cpu", ratio=2),
        Layout(name="fan", ratio=2),
    )

    layout["right"].split_column(
        Layout(name="led", ratio=1),
        Layout(name="info", ratio=1),
    )

    layout["cpu"].update(build_cpu_panel(data.get("monitor")))
    layout["fan"].update(build_fan_panel(data.get("fan")))
    layout["led"].update(build_led_panel(data.get("led")))
    layout["info"].update(build_info_panel(
        data.get("health"),
        data.get("monitor"),
        refresh_interval,
    ))

    return layout


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_top(
    base_url: str = "http://127.0.0.1:8420",
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    console: Console | None = None,
) -> None:
    """Run the interactive ``casectl top`` dashboard.

    Blocks until the user presses ``q`` or ``Ctrl+C``.

    Supports interactive controls:
    - ``m`` — cycle fan mode (follow-temp -> follow-rpi -> manual -> off)
    - ``+`` / ``=`` — increase fan speed by 10%
    - ``-`` — decrease fan speed by 10%
    - ``q`` — quit the dashboard

    Parameters
    ----------
    base_url:
        Daemon REST API base URL.
    refresh_interval:
        Seconds between data refreshes. Clamped to
        [MIN_REFRESH_INTERVAL, MAX_REFRESH_INTERVAL].
    console:
        Optional Rich Console for output (useful for testing).
    """
    from casectl.tui.input_handler import (
        KeyHandler,
        enter_raw_mode,
        exit_raw_mode,
        read_key_nonblocking,
    )

    refresh_interval = max(
        MIN_REFRESH_INTERVAL,
        min(refresh_interval, MAX_REFRESH_INTERVAL),
    )

    if console is None:
        console = Console()

    key_handler = KeyHandler(base_url)

    # Initial data fetch
    data = fetch_dashboard_data(base_url)
    layout = build_layout(data, refresh_interval, last_action=key_handler.last_action)

    _running = True

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal _running
        _running = False

    # Catch SIGINT gracefully
    old_handler = signal.signal(signal.SIGINT, _handle_signal)

    enter_raw_mode()
    try:
        with Live(
            layout,
            console=console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            last_fetch = time.monotonic()

            while _running and not key_handler.quit_requested:
                # Poll for keystrokes (non-blocking)
                key = read_key_nonblocking()
                if key is not None:
                    key_handler.handle_key(key, data)
                    if key_handler.quit_requested:
                        break
                    # Immediately refresh after a command to show feedback
                    data = fetch_dashboard_data(base_url)
                    layout = build_layout(
                        data,
                        refresh_interval,
                        last_action=key_handler.last_action,
                    )
                    live.update(layout)
                    last_fetch = time.monotonic()

                now = time.monotonic()
                if now - last_fetch >= refresh_interval:
                    data = fetch_dashboard_data(base_url)
                    layout = build_layout(
                        data,
                        refresh_interval,
                        last_action=key_handler.last_action,
                    )
                    live.update(layout)
                    last_fetch = now

                # Small sleep to avoid busy-waiting while staying responsive
                time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        exit_raw_mode()
        signal.signal(signal.SIGINT, old_handler)
