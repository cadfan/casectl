# casectl

Headless-first multi-interface controller for Freenove Computer Case Kit Pro (FNK0107 series) for Raspberry Pi.

## Architecture

Daemon-first, plugin-driven. All interfaces (CLI, Web, TUI, GUI) talk to the daemon via REST API. Built-in features are plugins that dogfood the plugin API.

```
CLI/Web/TUI → REST API (FastAPI :8420) → Plugin Host → Plugins → Hardware Abstraction → I2C/sysfs
```

## Project Structure

- `src/casectl/hardware/` — I2C drivers (expansion board 0x21, OLED 0x3C), system info (sysfs/psutil)
- `src/casectl/plugins/` — Built-in plugins: fan, led, oled, monitor, prometheus
- `src/casectl/daemon/` — FastAPI server, plugin host, event bus, runner
- `src/casectl/cli/` — Click CLI commands (talk to API via httpx)
- `src/casectl/web/` — HTMX + Jinja2 web dashboard
- `src/casectl/config/` — Pydantic v2 models + ruamel.yaml manager

## Testing

```bash
pytest                    # Run all tests
pytest --cov=casectl      # With coverage
```

Tests use mock hardware fixtures — no real I2C required.

## Key Conventions

- All I2C calls via `asyncio.to_thread()` (never block the event loop)
- Plugin API is 0.x (unstable until v1.0)
- Config at `~/.config/casectl/config.yaml` (XDG)
- API binds to 127.0.0.1 by default (opt-in LAN via config)
- Dark theme only in v0.1 (CSS custom properties for future themes)
- `ruamel.yaml` for config (preserves comments)
- Hardware deps are optional: `pip install casectl[hardware]`

## Hardware

- STM32 expansion board: I2C bus 1, address 0x21
- SSD1306 OLED: I2C bus 1, address 0x3C
- CPU temp: `/sys/devices/virtual/thermal/thermal_zone0/temp`
- Pi fan PWM: `/sys/devices/platform/cooling_fan/hwmon/hwmonX/pwm1`
