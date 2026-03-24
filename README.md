# casectl

Headless-first multi-interface controller for the Freenove FNK0107B Raspberry Pi case.

## Features

- **CLI, Web dashboard, and TUI interfaces** -- one daemon, multiple ways to interact
- **Plugin architecture** -- every feature (fan, LED, OLED, monitor) is a plugin
- **Fan control** with temperature-based auto mode, RPi fan-follow, manual, and off
- **RGB LED control** with rainbow, breathing, follow-temp, and manual colour modes
- **OLED display** with 4 cycling info screens (128x64 SSD1306)
- **System monitoring** with CPU, memory, disk, temperature, and fan speed metrics
- **Prometheus metrics endpoint** for Grafana dashboards
- **WebSocket real-time events** for live dashboards
- **Systemd service** with timer-based auto-start
- **Zero-config defaults** -- works out of the box

## Quick Start

```bash
pip install casectl[hardware]
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
casectl fan mode 0              # 0=follow_temp 1=follow_rpi 2=manual 3=custom 4=off
casectl fan speed 0 80          # Set channel 0 to 80% duty

# LED control
casectl led status
casectl led mode 0              # 0=rainbow 1=breathing 2=follow_temp 3=manual 4=custom 5=off
casectl led color 255 0 128     # Set RGB colour (switches to manual mode)

# OLED display
casectl oled status
casectl oled screen 0 --enable  # Enable/disable individual screens

# System metrics
casectl monitor

# Configuration
casectl config get fan
casectl config set fan mode follow_temp

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
    │  │   CLI    │  │  Web UI    │  │  WebSocket /ws    │   │
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
| `/api/ws`                               | WS     | Real-time event stream          |
| `/api/plugins/fan-control/status`       | GET    | Fan status, mode, duty, RPM     |
| `/api/plugins/fan-control/mode`         | POST   | Set fan mode                    |
| `/api/plugins/fan-control/speed`        | POST   | Set fan duty per channel        |
| `/api/plugins/led-control/status`       | GET    | LED mode, current colour        |
| `/api/plugins/led-control/mode`         | POST   | Set LED mode                    |
| `/api/plugins/led-control/color`        | POST   | Set RGB colour                  |
| `/api/plugins/oled-display/status`      | GET    | OLED screen info                |
| `/api/plugins/oled-display/screen`      | POST   | Enable/disable screens          |
| `/api/plugins/system-monitor/status`    | GET    | System metrics snapshot         |
| `/api/plugins/prometheus/metrics`       | GET    | Prometheus text format          |

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
- **Freenove FNK0107B case** with STM32-based expansion board
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

## Development

```bash
git clone <repo-url> casectl
cd casectl
pip install -e ".[all]"
pytest
ruff check src/
mypy src/casectl/
```

## License

MIT
