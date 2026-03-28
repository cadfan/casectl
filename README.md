# casectl

Headless-first multi-interface controller for the Freenove Computer Case Kit Pro (FNK0107 series) for Raspberry Pi.

**Documentation:** https://casectl.griffiths.cymru/

## Features

- **CLI, Web dashboard, and TUI interfaces** -- one daemon, multiple ways to interact
- **Interactive web dashboard** with mode dropdowns, colour picker, fan sliders, and OLED toggles
- **`casectl top`** -- live-updating terminal dashboard with interactive fan control
- **Plugin architecture** -- every feature is a plugin (8 built-in)
- **Fan control** with temperature-based auto mode, RPi fan-follow, manual, custom curves, and off
- **RGB LED control** with rainbow, breathing, follow-temp, and manual colour modes (16 named colours + hex)
- **OLED display** with 4 cycling info screens (128x64 SSD1306)
- **MQTT integration** with Home Assistant auto-discovery
- **Automation engine** -- event-driven rules with conditions, actions, and priority
- **Alerting** via webhook, ntfy.sh, and SMTP
- **System monitoring** with CPU, memory, disk, temperature, and fan speed metrics
- **Prometheus metrics endpoint** for Grafana dashboards
- **Real-time updates** via WebSocket (bidirectional) and Server-Sent Events
- **Systemd service** with timer-based auto-start
- **Zero-config defaults** -- works out of the box

## Quick Start

```bash
git clone https://github.com/cadfan/casectl.git && cd casectl
pip install -e ".[hardware]"
casectl doctor        # Check hardware and dependencies
casectl serve         # Start daemon + web dashboard
# Open http://localhost:8420 in your browser
```

## CLI Usage

```bash
# Status overview (default command)
casectl

# Fan control
casectl fan status
casectl fan mode follow-temp    # or: follow-rpi, manual, custom, off
casectl fan speed 0 80          # Set channel 0 to 80% duty

# LED control
casectl led status
casectl led mode rainbow        # or: breathing, follow-temp, manual, custom, off
casectl led color red            # Named colour (16 built-in names)
casectl led color '#FF0080'      # Hex code
casectl led color 255 0 128      # R G B values (0-255 each)

# OLED display
casectl oled status
casectl oled screen 0 --enable  # Enable/disable individual screens

# System metrics
casectl monitor

# Configuration
casectl config get fan
casectl config set fan mode 0   # Config stores integer enum values

# Interactive terminal dashboard
casectl top                     # Live-updating TUI (keys: m=mode, +/-=speed, q=quit)
casectl top --once              # Single snapshot for scripts

# Diagnostics
casectl doctor                  # Check hardware, I2C, dependencies

# API token management
casectl token                   # Show current API token

# Daemon management
casectl serve --bind 0.0.0.0 --port 8420 --log-level debug

# Systemd service
casectl service install         # Install + enable systemd units
casectl service start
casectl service stop
casectl service status
casectl service logs -f
casectl service uninstall
```

## Architecture

```
                       casectl daemon
    ┌─────────────────────────────────────────────────────────┐
    │                                                         │
    │  ┌──────────┐  ┌────────────┐  ┌───────────────────┐   │
    │  │   CLI    │  │  Web UI    │  │  WebSocket        │   │
    │  │  (Click) │  │  (FastAPI) │  │  (real-time)      │   │
    │  └────┬─────┘  └─────┬──────┘  └────────┬──────────┘   │
    │       │              │                   │              │
    │       └──────────────┼───────────────────┘              │
    │                      │                                  │
    │              ┌───────▼────────┐                          │
    │              │   REST API     │                          │
    │              │  /api/health   │                          │
    │              │  /api/plugins  │                          │
    │              │  /api/config   │                          │
    │              └───────┬────────┘                          │
    │                      │                                  │
    │  ┌───────────────────▼───────────────────────────────┐  │
    │  │              Plugin Host                          │  │
    │  │   discover -> load -> setup -> start -> stop      │  │
    │  └──┬──────┬──────┬──────┬──────┬────────────────────┘  │
    │     │      │      │      │      │                       │
    │  ┌──▼──┐┌──▼──┐┌──▼──┐┌─▼───┐┌─▼─────────┐            │
    │  │ Fan ││ LED ││OLED ││Mon- ││Prometheus │            │
    │  │Ctrl ││Ctrl ││Disp ││itor ││  /metrics │            │
    │  └──┬──┘└──┬──┘└──┬──┘└──┬──┘└───────────┘            │
    │  ┌──▼──┐┌──▼──────▼──┐┌──▼──────────────┐             │
    │  │MQTT ││ Automation ││   Alerting      │             │
    │  │(HA) ││  (rules)   ││(webhook/ntfy/…) │             │
    │  └──┬──┘└────────────┘└─────────────────┘             │
    │     │      │      │      │                              │
    │  ┌──▼──────▼──────▼──────▼──────────────────────────┐  │
    │  │              Event Bus                            │  │
    │  │  metrics_updated, daemon.started, daemon.stopping │  │
    │  └──────────────────────────────────────────────────┘   │
    │                      │                                  │
    │  ┌───────────────────▼───────────────────────────────┐  │
    │  │           Hardware Registry                       │  │
    │  │  ExpansionBoard (I2C 0x21)  │  OledDevice (0x3C) │  │
    │  │  SystemInfo (sysfs/psutil)                        │  │
    │  └──────────────────────────────────────────────────┘   │
    │                      │                                  │
    └──────────────────────┼──────────────────────────────────┘
                           │
              ┌────────────▼────────────┐
              │     Physical Hardware   │
              │  STM32 expansion board  │
              │  3x PWM fans            │
              │  4x RGB LEDs (WS2812)   │
              │  SSD1306 128x64 OLED    │
              │  Temperature sensor     │
              └─────────────────────────┘
```

## REST API

The daemon serves a REST API on port 8420 (default).

| Endpoint                                | Method | Description                     |
|-----------------------------------------|--------|---------------------------------|
| `/api/health`                           | GET    | Daemon health, uptime, plugins  |
| `/api/plugins`                          | GET    | List all loaded plugins         |
| `/api/config/{section}`                 | GET    | Read config section             |
| `/api/config`                           | PATCH  | Update config section           |
| `/api/ws`                               | WS     | Real-time event stream          |
| `/api/plugins/fan-control/status`       | GET    | Fan status, mode, duty, RPM     |
| `/api/plugins/fan-control/mode`         | PUT    | Set fan mode                    |
| `/api/plugins/fan-control/speed`        | PUT    | Set fan duty per channel        |
| `/api/plugins/led-control/status`       | GET    | LED mode, current colour        |
| `/api/plugins/led-control/mode`         | PUT    | Set LED mode                    |
| `/api/plugins/led-control/color`        | PUT    | Set RGB colour                  |
| `/api/plugins/oled-display/status`      | GET    | OLED screen info                |
| `/api/plugins/oled-display/screen`      | PUT    | Enable/disable screens          |
| `/api/plugins/oled-display/rotation`    | PUT    | Set display rotation (0 or 180) |
| `/api/plugins/oled-display/power`       | PUT    | Enable/disable OLED display     |
| `/api/plugins/oled-display/content`     | PUT    | Per-screen display settings     |
| `/api/plugins/system-monitor/status`    | GET    | System metrics snapshot         |
| `/api/plugins/prometheus/metrics`       | GET    | Prometheus text format          |
| `/api/plugins/mqtt/status`              | GET    | MQTT connection status          |
| `/api/plugins/automation/status`        | GET    | Automation engine status        |
| `/api/plugins/alerting/status`          | GET    | Alerting channel status         |
| `/api/sse`                              | GET    | Server-Sent Events stream       |

## Plugin Development

casectl uses a plugin architecture where every feature is a plugin.  Community
plugins are discovered automatically via Python entry points.

See [examples/plugins/casectl-example-plugin/](examples/plugins/casectl-example-plugin/)
for a complete example with step-by-step documentation.

The plugin lifecycle:

```
__init__() --> setup(ctx) --> start() --> [running] --> stop()
```

Plugins implement the `CasePlugin` protocol (structural subtyping -- no
inheritance required) and register an entry point:

```toml
[project.entry-points."casectl.plugins"]
my-plugin = "my_package.plugin:MyPlugin"
```

## Configuration

Config file location follows XDG:

```
~/.config/casectl/config.yaml
```

casectl works with zero configuration.  All settings have sensible defaults.
Configuration is read at startup and can be queried/modified via the API.

## Hardware

This project targets a specific hardware setup:

- **Raspberry Pi 5B** (also works on Pi 4B)
- **Freenove Computer Case Kit Pro (FNK0107 series)** with STM32-based expansion board
- **STM32 expansion board** at I2C address `0x21` -- drives 3 PWM fans, 4 RGB LEDs, and a temperature sensor
- **SSD1306 OLED display** at I2C address `0x3C` -- 128x64 pixels

### Enabling I2C

```bash
sudo raspi-config nonint do_i2c 0
sudo usermod -aG i2c $USER
# Log out and back in
casectl doctor   # Verify everything works
```

### Running without hardware

casectl degrades gracefully.  Without the expansion board or OLED, those
plugins report as degraded but the daemon, API, and monitoring still work.
This is useful for development on a regular Linux machine.

## Troubleshooting

**Cannot connect to daemon:** Run `casectl serve` first, or `casectl service install` for auto-start.

**I2C not detected:** Enable I2C with `sudo raspi-config nonint do_i2c 0` and reboot.

**Permission denied on I2C:** `sudo usermod -aG i2c $USER` then log out and back in.

**Expansion board not responding:** Check the ribbon cable. Run `casectl doctor` for diagnostics.

## Development

```bash
git clone https://github.com/cadfan/casectl.git && cd casectl
pip install -e ".[all]"
pytest
ruff check src/
mypy src/casectl/
```

## License

MIT
