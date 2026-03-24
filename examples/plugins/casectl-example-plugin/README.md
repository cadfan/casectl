# casectl Plugin Development Guide

This directory contains a complete example plugin that demonstrates the casectl
plugin API.  Use it as a starting point for your own plugins.

## Quick Start

```bash
cd examples/plugins/casectl-example-plugin
pip install -e .
casectl serve
# Your plugin is now loaded -- visit http://localhost:8420/api/plugins/example/status
```

## Creating a Plugin Step by Step

### 1. Set up the project structure

```
casectl-my-plugin/
    pyproject.toml
    casectl_my_plugin/
        __init__.py
        plugin.py
```

### 2. Write pyproject.toml

The key section is `[project.entry-points."casectl.plugins"]` -- this is how
casectl discovers your plugin at import time.

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "casectl-my-plugin"
version = "0.1.0"
description = "My custom casectl plugin"
requires-python = ">=3.11"
dependencies = ["casectl>=0.1.0"]

[project.entry-points."casectl.plugins"]
my-plugin = "casectl_my_plugin.plugin:MyPlugin"
```

The entry point format is:

```
<plugin-name> = "<python.module.path>:<ClassName>"
```

The plugin name (left side) is used for display only.  The actual plugin name
used for route prefixes and config keys comes from the `name` attribute on your
plugin class.

### 3. Implement the CasePlugin protocol

```python
from __future__ import annotations
from typing import Any
from fastapi import APIRouter
from casectl.plugins.base import PluginContext, PluginStatus


class MyPlugin:
    """My custom plugin."""

    name: str = "my-plugin"
    version: str = "0.1.0"
    description: str = "Does something useful"
    min_daemon_version: str = "0.1.0"

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes, config, commands, and event handlers."""
        ...

    async def start(self) -> None:
        """Begin background work."""
        ...

    async def stop(self) -> None:
        """Clean up resources."""
        ...

    def get_status(self) -> dict[str, Any]:
        """Report plugin health."""
        return {"status": PluginStatus.HEALTHY}
```

### 4. Install and run

```bash
pip install -e .
casectl serve
```

## The CasePlugin Protocol

casectl uses Python's `typing.Protocol` (structural subtyping) rather than
class inheritance.  Your plugin does NOT need to subclass anything -- it just
needs to have the right attributes and methods.

### Required attributes

| Attribute            | Type   | Description                                  |
|----------------------|--------|----------------------------------------------|
| `name`               | `str`  | Unique short name (e.g. `"my-plugin"`)       |
| `version`            | `str`  | SemVer version (e.g. `"0.1.0"`)             |
| `description`        | `str`  | One-line description                          |
| `min_daemon_version` | `str`  | Minimum casectl version required              |

### Required methods

| Method                       | Description                              |
|------------------------------|------------------------------------------|
| `async setup(ctx)`           | Receive context, register routes/events  |
| `async start()`              | Launch background tasks                  |
| `async stop()`               | Cancel tasks, release resources          |
| `get_status() -> dict`       | Return `{"status": PluginStatus, ...}`   |

### Lifecycle

```
__init__() --> setup(ctx) --> start() --> [running] --> stop()
```

1. **`__init__()`** -- The plugin host instantiates your class with no
   arguments.  Keep this lightweight.
2. **`setup(ctx)`** -- You receive a `PluginContext`.  Register your routes,
   config schema, CLI commands, and event subscriptions here.  Do NOT start
   background tasks yet.
3. **`start()`** -- All plugins are set up and the HTTP server is ready.
   Launch background tasks and begin real work.
4. **`stop()`** -- The daemon is shutting down.  Cancel tasks, close
   connections, flush buffers.  Plugins are stopped in reverse load order.

## PluginContext

The `PluginContext` is the only interface your plugin uses to interact with the
rest of casectl.  It is passed to `setup()` and you should store it for later
use.

### Route registration

```python
from fastapi import APIRouter

async def setup(self, ctx: PluginContext) -> None:
    router = APIRouter(tags=["my-plugin"])

    @router.get("/status")
    async def status():
        return {"ok": True}

    @router.post("/action")
    async def do_action(body: dict):
        return {"result": "done"}

    ctx.register_routes(router)
```

Routes are mounted at `/api/plugins/{plugin.name}/...` by the plugin host.
So the above creates:
- `GET /api/plugins/my-plugin/status`
- `POST /api/plugins/my-plugin/action`

### Config schema registration

```python
from pydantic import BaseModel

class MyConfig(BaseModel):
    threshold: float = 50.0
    enabled: bool = True

async def setup(self, ctx: PluginContext) -> None:
    ctx.register_config(MyConfig)
```

Users can then configure your plugin in `~/.config/casectl/config.yaml`:

```yaml
plugins:
  my-plugin:
    threshold: 65.0
    enabled: true
```

Read config at runtime:

```python
config = await ctx.get_config()  # Returns dict from plugins.my-plugin
threshold = config.get("threshold", 50.0)
```

### Event subscription

```python
async def setup(self, ctx: PluginContext) -> None:
    ctx.on_event("metrics_updated", self._on_metrics)
    ctx.on_event("daemon.started", self._on_started)

async def _on_metrics(self, data: dict) -> None:
    """Called every ~2s with system metrics."""
    cpu_temp = data.get("cpu_temp", 0)

async def _on_started(self, data: dict) -> None:
    """Called once when the daemon is fully ready."""
    version = data.get("version")
```

Common events emitted by casectl:

| Event               | Payload                        | Source          |
|---------------------|--------------------------------|-----------------|
| `metrics_updated`   | `SystemMetrics` dict           | system-monitor  |
| `daemon.started`    | `{"version": "0.1.0"}`         | daemon          |
| `daemon.stopping`   | `{}`                           | daemon          |

### Event emission

```python
ctx.emit_event("my-plugin.something_happened", {"value": 42})
```

Other plugins (or WebSocket subscribers) will receive the event.

### CLI command registration

```python
import click

async def setup(self, ctx: PluginContext) -> None:
    @click.group("my-plugin")
    def my_group():
        """My plugin commands."""

    @my_group.command("do-thing")
    def do_thing():
        click.echo("Done!")

    ctx.register_commands(my_group)
```

This adds `casectl my-plugin do-thing` to the CLI.

### Hardware access

```python
async def setup(self, ctx: PluginContext) -> None:
    hw = ctx.get_hardware()

    # Each may be None if not present
    expansion = hw.expansion       # STM32 board (I2C 0x21)
    oled = hw.oled                 # SSD1306 display (I2C 0x3C)
    system_info = hw.system_info   # CPU temp, memory, disk, etc.
```

Always check for `None` -- casectl runs gracefully without hardware.

### Logging

```python
async def setup(self, ctx: PluginContext) -> None:
    ctx.logger.info("Plugin is setting up")
    ctx.logger.debug("Detailed info for troubleshooting")
```

The logger is named `casectl.plugins.<name>` automatically.

## Entry-Point Discovery

casectl discovers community plugins using Python's standard entry-point
mechanism (`importlib.metadata.entry_points`).

When you define this in `pyproject.toml`:

```toml
[project.entry-points."casectl.plugins"]
example = "casectl_example.plugin:ExamplePlugin"
```

Python's packaging tools record it in the installed package metadata.  At
startup, casectl calls:

```python
from importlib.metadata import entry_points
eps = entry_points(group="casectl.plugins")
for ep in eps:
    plugin_cls = ep.load()  # Imports the module and returns the class
```

This means:
- Your plugin is discovered automatically when installed with `pip install`
- No need to edit casectl's config or source code
- Editable installs (`pip install -e .`) work too -- great for development
- Multiple plugins can be shipped in one package (multiple entry points)

## Testing Your Plugin

### Unit testing without the daemon

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from casectl.plugins.base import PluginContext, HardwareRegistry

@pytest.fixture
def ctx():
    return PluginContext(
        plugin_name="example",
        config_manager=None,
        hardware_registry=HardwareRegistry(),
        event_bus=MagicMock(),
    )

@pytest.mark.asyncio
async def test_setup(ctx):
    from casectl_example.plugin import ExamplePlugin

    plugin = ExamplePlugin()
    await plugin.setup(ctx)

    # Verify routes were registered
    assert ctx.routes is not None

@pytest.mark.asyncio
async def test_lifecycle(ctx):
    from casectl_example.plugin import ExamplePlugin

    plugin = ExamplePlugin()
    await plugin.setup(ctx)
    await plugin.start()

    status = plugin.get_status()
    assert status["status"].value == "healthy"

    await plugin.stop()
    status = plugin.get_status()
    assert status["status"].value == "stopped"
```

### Integration testing with the daemon

```bash
# Install the plugin in editable mode
pip install -e .

# Start the daemon with debug logging
casectl serve --log-level debug

# In another terminal, test the API
curl http://localhost:8420/api/plugins/example/status
curl http://localhost:8420/api/plugins  # Lists all loaded plugins
curl http://localhost:8420/api/health   # Shows plugin in health check
```

### Verifying entry-point registration

```bash
# Check that Python can see your entry point
python -c "
from importlib.metadata import entry_points
eps = entry_points(group='casectl.plugins')
for ep in eps:
    print(f'{ep.name} = {ep.value}')
"
```

## Tips

- **Keep `__init__` lightweight** -- no I/O, no async, no side effects.
- **Store the context** in `setup()` -- you will need it in `start()` and
  event handlers.
- **Check hardware for None** -- your plugin should work (perhaps in degraded
  mode) without the Freenove case hardware attached.
- **Use `PluginStatus.DEGRADED`** when something is wrong but the plugin can
  still partially function.
- **Catch your own exceptions** in background tasks -- one unhandled exception
  will kill the task silently.
- **Use `ctx.logger`** rather than `print()` or your own logger -- it
  integrates with casectl's structured logging.
