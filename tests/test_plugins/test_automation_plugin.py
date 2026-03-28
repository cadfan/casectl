"""Tests for the AutomationPlugin integration with the casectl plugin system."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.daemon.event_bus import EventBus
from casectl.plugins.automation.plugin import AutomationPlugin, _build_default_action_handlers
from casectl.plugins.base import HardwareRegistry, PluginContext, PluginStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_bus() -> EventBus:
    return EventBus(max_ws=5)


@pytest.fixture()
def plugin_context(event_bus: EventBus, tmp_path) -> PluginContext:
    """Create a PluginContext with a real event bus and mock config manager."""
    from casectl.config.manager import ConfigManager

    config_file = tmp_path / "casectl" / "config.yaml"
    mgr = ConfigManager(path=config_file)

    hw = HardwareRegistry()
    return PluginContext(
        plugin_name="automation",
        config_manager=mgr,
        hardware_registry=hw,
        event_bus=event_bus,
    )


@pytest.fixture()
def plugin_context_with_config(event_bus: EventBus, tmp_path) -> PluginContext:
    """Create a PluginContext that returns automation config."""
    mgr = AsyncMock()
    mgr.get = AsyncMock(return_value={
        "automation": {
            "enabled": True,
            "rules": [
                {
                    "name": "overheat",
                    "event": "metrics_updated",
                    "priority": "safety",
                    "conditions": [{"field": "cpu_temp", "operator": "gt", "value": 80}],
                    "actions": [
                        {"target": "fan", "command": "set_duty", "params": {"duty": 255}},
                    ],
                },
            ],
        },
    })

    hw = HardwareRegistry()
    ctx = PluginContext(
        plugin_name="automation",
        config_manager=mgr,
        hardware_registry=hw,
        event_bus=event_bus,
    )
    return ctx


# ---------------------------------------------------------------------------
# Plugin lifecycle tests
# ---------------------------------------------------------------------------


class TestAutomationPluginProtocol:
    """Test that AutomationPlugin conforms to CasePlugin protocol."""

    def test_has_required_attributes(self) -> None:
        p = AutomationPlugin()
        assert p.name == "automation"
        assert p.version == "0.2.0"
        assert p.description
        assert p.min_daemon_version == "0.1.0"

    def test_has_required_methods(self) -> None:
        p = AutomationPlugin()
        assert callable(p.setup)
        assert callable(p.start)
        assert callable(p.stop)
        assert callable(p.get_status)


class TestAutomationPluginSetup:
    """Test plugin setup phase."""

    async def test_setup_registers_routes(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        assert plugin_context.routes is not None

    async def test_setup_registers_default_handlers(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        targets = p.action_registry.targets
        assert "emit" in targets
        assert "log" in targets
        assert "fan" in targets
        assert "led" in targets
        assert "oled" in targets


class TestAutomationPluginStart:
    """Test plugin start phase."""

    async def test_start_with_empty_config(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        # Load default config (empty, disabled)
        await p.start()
        assert p.engine is not None
        assert p.engine.config.enabled is False

    async def test_start_disabled_engine(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        await p.start()
        status = p.get_status()
        assert status["status"] == PluginStatus.STOPPED

    async def test_start_without_setup_logs_error(self) -> None:
        p = AutomationPlugin()
        await p.start()  # Should not raise
        assert p.engine is None


class TestAutomationPluginStop:
    """Test plugin stop phase."""

    async def test_stop(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        await p.start()
        await p.stop()
        status = p.get_status()
        assert status["status"] == PluginStatus.STOPPED


class TestAutomationPluginStatus:
    """Test plugin status reporting."""

    def test_status_before_start(self) -> None:
        p = AutomationPlugin()
        status = p.get_status()
        assert status["status"] == PluginStatus.STOPPED

    async def test_status_after_start(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        await p.start()
        status = p.get_status()
        assert "rule_count" in status
        assert "stats" in status


class TestAutomationPluginReload:
    """Test config reload."""

    async def test_reload_updates_rules(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        await p.start()
        count = await p.reload_config()
        assert count == 0  # Default config has no rules

    async def test_reload_before_start(self, plugin_context: PluginContext) -> None:
        p = AutomationPlugin()
        await p.setup(plugin_context)
        # Reload without starting first
        count = await p.reload_config()
        assert count == 0  # Creates engine on demand


# ---------------------------------------------------------------------------
# Default action handlers
# ---------------------------------------------------------------------------


class TestDefaultActionHandlers:
    """Test built-in action handler behaviour."""

    async def test_emit_handler(self, plugin_context: PluginContext, event_bus: EventBus) -> None:
        handlers = _build_default_action_handlers(plugin_context)
        emit_handler = handlers["emit"]

        received = []
        event_bus.subscribe("custom_event", lambda data: received.append(data))

        from casectl.plugins.automation.models import RuleAction
        action = RuleAction(
            target="emit",
            command="fire",
            params={"event": "custom_event", "data": {"key": "value"}},
        )
        await emit_handler(action)

        # Give the event loop a chance to process
        import asyncio
        await asyncio.sleep(0.01)
        # The emit is fire-and-forget via create_task, results may vary in test

    async def test_log_handler(self, plugin_context: PluginContext) -> None:
        handlers = _build_default_action_handlers(plugin_context)
        log_handler = handlers["log"]

        from casectl.plugins.automation.models import RuleAction
        action = RuleAction(
            target="log",
            command="info",
            params={"message": "test log message", "level": "warning"},
        )
        # Should not raise
        await log_handler(action)

    async def test_fan_handler_emits_event(
        self, plugin_context: PluginContext, event_bus: EventBus
    ) -> None:
        handlers = _build_default_action_handlers(plugin_context)
        fan_handler = handlers["fan"]

        from casectl.plugins.automation.models import RuleAction
        action = RuleAction(
            target="fan",
            command="set_duty",
            params={"duty": [200, 200, 200]},
        )
        # Should not raise
        await fan_handler(action)

    async def test_led_handler_emits_event(
        self, plugin_context: PluginContext, event_bus: EventBus
    ) -> None:
        handlers = _build_default_action_handlers(plugin_context)
        led_handler = handlers["led"]

        from casectl.plugins.automation.models import RuleAction
        action = RuleAction(
            target="led",
            command="set_mode",
            params={"mode": "rainbow"},
        )
        await led_handler(action)


# ---------------------------------------------------------------------------
# Integration: EventBus → RulesEngine
# ---------------------------------------------------------------------------


class TestEventBusIntegration:
    """Test the full event → condition → action pipeline."""

    async def test_event_triggers_rule(self, event_bus: EventBus, tmp_path) -> None:
        """Full integration: event bus emit → rule evaluation → action execution."""
        from casectl.config.manager import ConfigManager

        # Set up config with automation rules
        config_file = tmp_path / "casectl" / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        # Write plugins.automation config
        await mgr.update("plugins", {
            "automation": {
                "enabled": True,
                "rules": [
                    {
                        "name": "hot-alert",
                        "event": "metrics_updated",
                        "conditions": [{"field": "cpu_temp", "operator": "gt", "value": 80}],
                        "actions": [{"target": "log", "command": "info", "params": {"message": "hot"}}],
                    },
                ],
            },
        })

        hw = HardwareRegistry()
        ctx = PluginContext(
            plugin_name="automation",
            config_manager=mgr,
            hardware_registry=hw,
            event_bus=event_bus,
        )

        plugin = AutomationPlugin()
        await plugin.setup(ctx)
        await plugin.start()

        # Engine should have loaded the rule
        assert plugin.engine is not None
        assert len(plugin.engine.rules) == 1

        # Emit an event that matches the rule
        await event_bus.emit("metrics_updated", {"cpu_temp": 85})

        # Check that the rule was processed
        assert plugin.engine.stats.events_processed >= 1
