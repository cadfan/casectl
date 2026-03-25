"""casectl CLI — command-line interface for the casectl daemon.

Communicates with the daemon over its REST API using httpx (synchronous).
All output is formatted with Rich.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_HOST = "http://127.0.0.1:8420"


def _base_url(ctx: click.Context) -> str:
    """Return the daemon base URL from the Click context."""
    return ctx.obj["host"]


def _client(ctx: click.Context) -> httpx.Client:
    """Build a one-shot httpx client pointed at the daemon."""
    return httpx.Client(base_url=_base_url(ctx), timeout=10.0)


def _api_get(ctx: click.Context, path: str) -> dict:
    """GET *path* from the daemon API and return parsed JSON."""
    try:
        with _client(ctx) as client:
            resp = client.get(path)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        err_console.print(
            "[bold red]Cannot connect to casectl daemon. "
            "Is 'casectl serve' running?[/]"
        )
        raise SystemExit(1)
    except httpx.HTTPStatusError as exc:
        err_console.print(f"[bold red]API error:[/] {exc.response.status_code} — {exc.response.text}")
        raise SystemExit(1)


def _api_post(ctx: click.Context, path: str, json: dict) -> dict:
    """POST JSON to *path* on the daemon API and return parsed JSON."""
    try:
        with _client(ctx) as client:
            resp = client.post(path, json=json)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        err_console.print(
            "[bold red]Cannot connect to casectl daemon. "
            "Is 'casectl serve' running?[/]"
        )
        raise SystemExit(1)
    except httpx.HTTPStatusError as exc:
        err_console.print(f"[bold red]API error:[/] {exc.response.status_code} — {exc.response.text}")
        raise SystemExit(1)


def _api_patch(ctx: click.Context, path: str, json: dict) -> dict:
    """PATCH JSON to *path* on the daemon API and return parsed JSON."""
    try:
        with _client(ctx) as client:
            resp = client.patch(path, json=json)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        err_console.print(
            "[bold red]Cannot connect to casectl daemon. "
            "Is 'casectl serve' running?[/]"
        )
        raise SystemExit(1)
    except httpx.HTTPStatusError as exc:
        err_console.print(f"[bold red]API error:[/] {exc.response.status_code} — {exc.response.text}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Root CLI group
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option(
    "--host",
    default=_DEFAULT_HOST,
    envvar="CASECTL_HOST",
    show_default=True,
    help="Daemon API base URL.",
)
@click.pass_context
def cli(ctx: click.Context, host: str) -> None:
    """casectl — headless controller for Freenove FNK0107B case hardware."""
    ctx.ensure_object(dict)
    ctx.obj["host"] = host.rstrip("/")

    if ctx.invoked_subcommand is None:
        _print_status_summary(ctx)


def _print_status_summary(ctx: click.Context) -> None:
    """Default action: fetch health + key metrics and pretty-print."""
    health = _api_get(ctx, "/api/health")

    table = Table(title="casectl status", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Status", f"[green]{health.get('status', 'unknown')}[/green]")
    table.add_row("Version", str(health.get("version", "?")))
    uptime = health.get("uptime", 0)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    table.add_row("Uptime", f"{hours}h {minutes}m {seconds}s")

    plugins = health.get("plugins", [])
    for plugin in plugins:
        name = plugin.get("name", "?")
        status = plugin.get("status", "unknown")
        colour = "green" if status == "running" else "red"
        table.add_row(f"Plugin: {name}", f"[{colour}]{status}[/{colour}]")

    console.print(table)


# ===================================================================
# Token command
# ===================================================================


@cli.command("token")
def show_token() -> None:
    """Display the current API access token."""
    from pathlib import Path
    import os
    token_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "casectl" / ".api-token"
    if token_path.exists():
        token = token_path.read_text().strip()
        console.print(f"[green]API Token:[/green] {token}")
        console.print(f"[dim]File:[/dim] {token_path}")
    else:
        console.print("[yellow]No token file found.[/yellow] Token is only generated when binding to a non-localhost address.")


# ===================================================================
# Fan commands
# ===================================================================


@cli.group()
@click.pass_context
def fan(ctx: click.Context) -> None:
    """Fan control commands."""


@fan.command("status")
@click.pass_context
def fan_status(ctx: click.Context) -> None:
    """Show current fan status."""
    data = _api_get(ctx, "/api/plugins/fan-control/status")

    table = Table(title="Fan Status", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    mode = data.get("mode", "unknown")
    degraded = data.get("degraded", False)

    table.add_row("Mode", mode)
    table.add_row("CPU Temp", f"{data.get('temp', 0):.1f} C")
    table.add_row(
        "Health",
        "[red]DEGRADED[/red]" if degraded else "[green]OK[/green]",
    )

    duty = data.get("duty", [])
    rpm = data.get("rpm", [])
    for i in range(max(len(duty), len(rpm))):
        d = duty[i] if i < len(duty) else 0
        r = rpm[i] if i < len(rpm) else 0
        table.add_row(f"Channel {i}", f"duty={d}  rpm={r}")

    console.print(table)


_FAN_MODE_NAMES: dict[str, int] = {
    "follow-temp": 0, "follow-rpi": 1, "manual": 2, "custom": 3, "off": 4,
}


@fan.command("mode")
@click.argument("mode", type=str)
@click.pass_context
def fan_mode(ctx: click.Context, mode: str) -> None:
    """Set fan mode (follow-temp, follow-rpi, manual, custom, off)."""
    mode_int = _FAN_MODE_NAMES.get(mode.lower())
    if mode_int is None:
        try:
            mode_int = int(mode)
        except ValueError:
            console.print(f"[red]Unknown mode:[/red] {mode}")
            console.print(f"Valid modes: {', '.join(_FAN_MODE_NAMES)}")
            raise SystemExit(1)
    data = _api_post(ctx, "/api/plugins/fan-control/mode", {"mode": mode_int})
    console.print(f"[green]Fan mode set to:[/green] {data.get('mode', mode)}")


@fan.command("speed")
@click.argument("channel", type=int)
@click.argument("duty", type=int)
@click.pass_context
def fan_speed(ctx: click.Context, channel: int, duty: int) -> None:
    """Set fan speed for a channel (duty 0-100).

    Sets the specified channel's duty cycle. Other channels are padded
    from the last provided value by the API.
    """
    if not 0 <= duty <= 100:
        err_console.print("[bold red]Duty must be 0-100.[/]")
        raise SystemExit(1)

    # Build a duty list with the correct channel set.
    duty_list = [0] * (channel + 1)
    duty_list[channel] = duty
    data = _api_post(ctx, "/api/plugins/fan-control/speed", {"duty": duty_list})
    console.print(f"[green]Fan speed set.[/green] Hardware duty: {data.get('duty_hw', duty_list)}")


# ===================================================================
# LED commands
# ===================================================================


@cli.group()
@click.pass_context
def led(ctx: click.Context) -> None:
    """LED control commands."""


@led.command("status")
@click.pass_context
def led_status(ctx: click.Context) -> None:
    """Show current LED status."""
    data = _api_get(ctx, "/api/plugins/led-control/status")

    table = Table(title="LED Status", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Mode", data.get("mode", "unknown"))

    color = data.get("color", {})
    r, g, b = color.get("red", 0), color.get("green", 0), color.get("blue", 0)
    color_text = Text(f"rgb({r}, {g}, {b})")
    color_text.stylize(f"on rgb({r},{g},{b})")
    table.add_row("Color", color_text)

    degraded = data.get("degraded", False)
    table.add_row(
        "Health",
        "[red]DEGRADED[/red]" if degraded else "[green]OK[/green]",
    )

    console.print(table)


_LED_MODE_NAMES: dict[str, int] = {
    "rainbow": 0, "breathing": 1, "follow-temp": 2, "manual": 3, "custom": 4, "off": 5,
}


@led.command("mode")
@click.argument("mode", type=str)
@click.pass_context
def led_mode(ctx: click.Context, mode: str) -> None:
    """Set LED mode (rainbow, breathing, follow-temp, manual, custom, off)."""
    mode_int = _LED_MODE_NAMES.get(mode.lower())
    if mode_int is None:
        try:
            mode_int = int(mode)
        except ValueError:
            console.print(f"[red]Unknown mode:[/red] {mode}")
            console.print(f"Valid modes: {', '.join(_LED_MODE_NAMES)}")
            raise SystemExit(1)
    data = _api_post(ctx, "/api/plugins/led-control/mode", {"mode": mode_int})
    console.print(f"[green]LED mode set to:[/green] {data.get('mode', mode)}")


@led.command("color")
@click.argument("r", type=int)
@click.argument("g", type=int)
@click.argument("b", type=int)
@click.pass_context
def led_color(ctx: click.Context, r: int, g: int, b: int) -> None:
    """Set LED colour (R G B, each 0-255). Switches to manual mode."""
    for name, val in [("R", r), ("G", g), ("B", b)]:
        if not 0 <= val <= 255:
            err_console.print(f"[bold red]{name} must be 0-255, got {val}.[/]")
            raise SystemExit(1)

    data = _api_post(ctx, "/api/plugins/led-control/color", {"red": r, "green": g, "blue": b})
    color = data.get("color", {})
    console.print(
        f"[green]LED colour set to:[/green] "
        f"rgb({color.get('red', r)}, {color.get('green', g)}, {color.get('blue', b)})"
    )


# ===================================================================
# OLED commands
# ===================================================================


@cli.group()
@click.pass_context
def oled(ctx: click.Context) -> None:
    """OLED display commands."""


@oled.command("status")
@click.pass_context
def oled_status(ctx: click.Context) -> None:
    """Show current OLED display status."""
    data = _api_get(ctx, "/api/plugins/oled-display/status")

    table = Table(title="OLED Display Status", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Current Screen", str(data.get("current_screen", 0)))
    table.add_row("Rotation", f"{data.get('rotation', 0)} deg")

    degraded = data.get("degraded", False)
    table.add_row(
        "Health",
        "[red]DEGRADED[/red]" if degraded else "[green]OK[/green]",
    )

    screen_names = data.get("screen_names", [])
    screens_enabled = data.get("screens_enabled", [])
    for i, name in enumerate(screen_names):
        enabled = screens_enabled[i] if i < len(screens_enabled) else False
        status_text = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"
        table.add_row(f"Screen {i}: {name}", status_text)

    console.print(table)


@oled.command("screen")
@click.argument("index", type=int)
@click.option("--enable/--disable", required=True, help="Enable or disable the screen.")
@click.pass_context
def oled_screen(ctx: click.Context, index: int, enable: bool) -> None:
    """Enable or disable an OLED screen by index."""
    data = _api_post(
        ctx,
        "/api/plugins/oled-display/screen",
        {"index": index, "enabled": enable},
    )
    action = "enabled" if data.get("enabled", enable) else "disabled"
    console.print(f"[green]Screen {data.get('index', index)} {action}.[/green]")


# ===================================================================
# Monitor command
# ===================================================================


@cli.command("monitor")
@click.pass_context
def monitor(ctx: click.Context) -> None:
    """Show system metrics from the daemon."""
    data = _api_get(ctx, "/api/plugins/system-monitor/status")

    table = Table(title="System Monitor", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value")

    table.add_row("CPU Usage", f"{data.get('cpu_percent', 0):.1f}%")
    table.add_row("Memory Usage", f"{data.get('memory_percent', 0):.1f}%")
    table.add_row("Disk Usage", f"{data.get('disk_percent', 0):.1f}%")
    table.add_row("CPU Temp", f"{data.get('cpu_temp', 0):.1f} C")
    table.add_row("Case Temp", f"{data.get('case_temp', 0):.1f} C")
    table.add_row("IP Address", data.get("ip_address", "n/a"))

    fan_duty = data.get("fan_duty", [])
    motor_speed = data.get("motor_speed", [])
    for i in range(max(len(fan_duty), len(motor_speed))):
        d = fan_duty[i] if i < len(fan_duty) else 0
        r = motor_speed[i] if i < len(motor_speed) else 0
        table.add_row(f"Fan {i}", f"duty={d}  rpm={r}")

    console.print(table)


# ===================================================================
# Config commands
# ===================================================================


@cli.group()
@click.pass_context
def config(ctx: click.Context) -> None:
    """Configuration commands."""


@config.command("get")
@click.argument("section")
@click.pass_context
def config_get(ctx: click.Context, section: str) -> None:
    """Get a configuration section (e.g. fan, led, oled, service)."""
    data = _api_get(ctx, f"/api/config/{section}")

    table = Table(
        title=f"Config: {section}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Key", style="bold")
    table.add_column("Value")

    for key, value in data.items():
        table.add_row(str(key), str(value))

    console.print(table)


@config.command("set")
@click.argument("section")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, section: str, key: str, value: str) -> None:
    """Set a configuration value: casectl config set <section> <key> <value>."""
    # Attempt to coerce the value to an appropriate Python type.
    coerced: int | float | bool | str = value
    if value.lower() in ("true", "false"):
        coerced = value.lower() == "true"
    else:
        try:
            coerced = int(value)
        except ValueError:
            try:
                coerced = float(value)
            except ValueError:
                pass  # keep as string

    data = _api_patch(ctx, "/api/config", {"section": section, key: coerced})
    console.print(f"[green]Config updated:[/green] {section}.{key} = {coerced}")


# ===================================================================
# Serve command
# ===================================================================


@cli.command("serve")
@click.option("--bind", default=None, help="Bind address (default from config).")
@click.option("--port", default=None, type=int, help="Bind port (default from config).")
@click.option("--log-level", default="info", help="Logging level.")
def serve(bind: str | None, port: int | None, log_level: str) -> None:
    """Start the casectl daemon."""
    import asyncio

    from casectl.daemon.runner import run_daemon

    console.print("[bold cyan]Starting casectl daemon...[/bold cyan]")
    try:
        asyncio.run(run_daemon(host=bind, port=port, log_level=log_level))
    except KeyboardInterrupt:
        console.print("\n[yellow]Daemon stopped.[/yellow]")


# ===================================================================
# Doctor command (runs locally — no daemon needed)
# ===================================================================


@cli.command("doctor")
def doctor() -> None:
    """Run local hardware and dependency checks (no daemon needed)."""
    table = Table(title="casectl doctor", show_header=True, header_style="bold cyan")
    table.add_column("Check", style="bold", min_width=35)
    table.add_column("Result", min_width=10)
    table.add_column("Details")

    checks_passed = 0
    checks_total = 0

    def _pass(name: str, detail: str = "") -> None:
        nonlocal checks_passed, checks_total
        checks_total += 1
        checks_passed += 1
        table.add_row(name, "[green]PASS[/green]", detail)

    def _fail(name: str, detail: str) -> None:
        nonlocal checks_total
        checks_total += 1
        table.add_row(name, "[red]FAIL[/red]", f"[red]{detail}[/red]")

    # 1. Python version >= 3.11
    v = sys.version_info
    if v >= (3, 11):
        _pass("Python >= 3.11", f"{v.major}.{v.minor}.{v.micro}")
    else:
        _fail("Python >= 3.11", f"Found {v.major}.{v.minor}.{v.micro}. Upgrade to 3.11+.")

    # 2. smbus2 importable
    try:
        import smbus2  # noqa: F401

        _pass("smbus2 importable")
    except ImportError:
        _fail("smbus2 importable", "pip install smbus2")

    # 3. /dev/i2c-1 exists
    i2c_path = "/dev/i2c-1"
    if os.path.exists(i2c_path):
        _pass("/dev/i2c-1 exists")
    else:
        _fail(
            "/dev/i2c-1 exists",
            "I2C not enabled. Run: sudo raspi-config nonint do_i2c 0",
        )

    # 4. /dev/i2c-1 permissions (user in i2c group)
    if os.path.exists(i2c_path) and os.access(i2c_path, os.R_OK | os.W_OK):
        _pass("/dev/i2c-1 permissions")
    else:
        import grp

        user = os.environ.get("USER", "unknown")
        try:
            i2c_members = grp.getgrnam("i2c").gr_mem
            if user in i2c_members:
                _fail(
                    "/dev/i2c-1 permissions",
                    "User is in i2c group but cannot access device. Check udev rules.",
                )
            else:
                _fail(
                    "/dev/i2c-1 permissions",
                    f"Add user to i2c group: sudo usermod -aG i2c {user} && logout",
                )
        except KeyError:
            _fail(
                "/dev/i2c-1 permissions",
                f"i2c group does not exist. Run: sudo groupadd i2c && sudo usermod -aG i2c {user}",
            )

    # 5. I2C probe 0x21 (STM32 expansion board)
    try:
        import smbus2

        bus = smbus2.SMBus(1)
        try:
            bus.read_byte(0x21)
            _pass("I2C probe 0x21 (STM32)", "Device responding")
        except OSError:
            _fail(
                "I2C probe 0x21 (STM32)",
                "No device at 0x21. Check expansion board connection.",
            )
        finally:
            bus.close()
    except Exception:
        _fail(
            "I2C probe 0x21 (STM32)",
            "Cannot open I2C bus. Ensure I2C is enabled and accessible.",
        )

    # 6. I2C probe 0x3C (OLED display)
    try:
        import smbus2

        bus = smbus2.SMBus(1)
        try:
            bus.read_byte(0x3C)
            _pass("I2C probe 0x3C (OLED)", "Device responding")
        except OSError:
            _fail(
                "I2C probe 0x3C (OLED)",
                "No device at 0x3C. Check OLED display connection.",
            )
        finally:
            bus.close()
    except Exception:
        _fail(
            "I2C probe 0x3C (OLED)",
            "Cannot open I2C bus. Ensure I2C is enabled and accessible.",
        )

    # 7. CPU temp sysfs readable
    cpu_temp_path = "/sys/class/thermal/thermal_zone0/temp"
    try:
        with open(cpu_temp_path) as f:
            raw = f.read().strip()
            temp_c = int(raw) / 1000.0
        _pass("CPU temp sysfs readable", f"{temp_c:.1f} C")
    except FileNotFoundError:
        _fail("CPU temp sysfs readable", f"{cpu_temp_path} not found.")
    except (ValueError, OSError) as exc:
        _fail("CPU temp sysfs readable", str(exc))

    # 8. luma.oled importable
    try:
        from luma.oled.device import ssd1306  # noqa: F401

        _pass("luma.oled importable")
    except ImportError:
        _fail("luma.oled importable", "pip install luma.oled")

    # 9. Pillow importable
    try:
        from PIL import Image  # noqa: F401

        _pass("Pillow importable")
    except ImportError:
        _fail("Pillow importable", "pip install Pillow")

    console.print(table)
    console.print()
    if checks_passed == checks_total:
        console.print(
            Panel(
                f"[bold green]All {checks_total} checks passed.[/bold green]",
                border_style="green",
            )
        )
    else:
        failed = checks_total - checks_passed
        console.print(
            Panel(
                f"[bold red]{failed} of {checks_total} checks failed.[/bold red] "
                "See details above for fixes.",
                border_style="red",
            )
        )


# ===================================================================
# Service commands
# ===================================================================

_SYSTEMD_SERVICE = textwrap.dedent("""\
    [Unit]
    Description=casectl daemon — Freenove case hardware controller
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    ExecStart={exec_start}
    Restart=on-failure
    RestartSec=5
    User={user}
    Environment=PYTHONUNBUFFERED=1

    [Install]
    WantedBy=multi-user.target
""")

_SYSTEMD_TIMER = textwrap.dedent("""\
    [Unit]
    Description=Start casectl daemon after boot delay

    [Timer]
    OnBootSec=10s
    Unit=casectl.service

    [Install]
    WantedBy=timers.target
""")

_SERVICE_PATH = "/etc/systemd/system/casectl.service"
_TIMER_PATH = "/etc/systemd/system/casectl.timer"


@cli.group()
@click.pass_context
def service(ctx: click.Context) -> None:
    """Manage the casectl systemd service."""


@service.command("install")
def service_install() -> None:
    """Generate and install casectl systemd service and timer units."""
    import shutil

    casectl_bin = shutil.which("casectl")
    if casectl_bin is None:
        # Fall back to the current Python with -m
        casectl_bin = f"{sys.executable} -m casectl.cli.main"

    user = os.environ.get("USER", "root")

    service_content = _SYSTEMD_SERVICE.format(
        exec_start=f"{casectl_bin} serve",
        user=user,
    )
    timer_content = _SYSTEMD_TIMER

    console.print("[bold cyan]Installing systemd units...[/bold cyan]")

    # Write service unit via sudo tee
    try:
        proc = subprocess.run(
            ["sudo", "tee", _SERVICE_PATH],
            input=service_content.encode(),
            capture_output=True,
            check=True,
        )
        console.print(f"  [green]Wrote[/green] {_SERVICE_PATH}")
    except subprocess.CalledProcessError as exc:
        err_console.print(f"[bold red]Failed to write {_SERVICE_PATH}:[/bold red] {exc.stderr.decode()}")
        raise SystemExit(1)

    # Write timer unit via sudo tee
    try:
        proc = subprocess.run(
            ["sudo", "tee", _TIMER_PATH],
            input=timer_content.encode(),
            capture_output=True,
            check=True,
        )
        console.print(f"  [green]Wrote[/green] {_TIMER_PATH}")
    except subprocess.CalledProcessError as exc:
        err_console.print(f"[bold red]Failed to write {_TIMER_PATH}:[/bold red] {exc.stderr.decode()}")
        raise SystemExit(1)

    # Reload systemd
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    console.print("  [green]Reloaded systemd daemon.[/green]")

    # Enable timer
    subprocess.run(["sudo", "systemctl", "enable", "casectl.timer"], check=True)
    console.print("  [green]Enabled casectl.timer.[/green]")

    console.print()
    console.print(
        Panel(
            "[bold green]Installation complete.[/bold green]\n"
            "The daemon will start automatically 10s after boot via the timer.\n"
            "To start now: [cyan]casectl service start[/cyan]",
            border_style="green",
        )
    )


@service.command("uninstall")
def service_uninstall() -> None:
    """Stop, disable, and remove casectl systemd units."""
    console.print("[bold cyan]Uninstalling casectl systemd units...[/bold cyan]")

    # Stop and disable
    subprocess.run(
        ["sudo", "systemctl", "stop", "casectl.timer", "casectl.service"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "disable", "casectl.timer", "casectl.service"],
        capture_output=True,
    )

    # Remove unit files
    for path in (_SERVICE_PATH, _TIMER_PATH):
        try:
            subprocess.run(["sudo", "rm", "-f", path], check=True)
            console.print(f"  [green]Removed[/green] {path}")
        except subprocess.CalledProcessError:
            err_console.print(f"  [yellow]Could not remove {path}[/yellow]")

    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    console.print("  [green]Reloaded systemd daemon.[/green]")
    console.print(Panel("[bold green]Uninstallation complete.[/bold green]", border_style="green"))


@service.command("start")
def service_start() -> None:
    """Start the casectl daemon via systemd."""
    subprocess.run(["sudo", "systemctl", "start", "casectl.service"], check=True)
    console.print("[green]casectl service started.[/green]")


@service.command("stop")
def service_stop() -> None:
    """Stop the casectl daemon via systemd."""
    subprocess.run(["sudo", "systemctl", "stop", "casectl.service"], check=True)
    console.print("[yellow]casectl service stopped.[/yellow]")


@service.command("restart")
def service_restart() -> None:
    """Restart the casectl daemon via systemd."""
    subprocess.run(["sudo", "systemctl", "restart", "casectl.service"], check=True)
    console.print("[green]casectl service restarted.[/green]")


@service.command("status")
def service_status() -> None:
    """Show casectl systemd service status."""
    result = subprocess.run(
        ["systemctl", "status", "casectl.service"],
        capture_output=True,
        text=True,
    )
    # systemctl status returns non-zero if the service is not running,
    # which is not an error for our purposes — always print output.
    output = result.stdout or result.stderr or "No output from systemctl."
    console.print(Panel(output.strip(), title="casectl.service", border_style="cyan"))


@service.command("logs")
@click.option("-n", "--lines", default=50, help="Number of log lines to show.")
@click.option("-f", "--follow", is_flag=True, help="Follow log output.")
def service_logs(lines: int, follow: bool) -> None:
    """Show casectl daemon logs from journald."""
    cmd = ["journalctl", "-u", "casectl.service", f"--lines={lines}", "--no-pager"]
    if follow:
        cmd.append("--follow")
    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
