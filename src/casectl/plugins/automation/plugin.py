"""Automation plugin — integrates the rules engine with the casectl plugin system.

Loads rules from config.yaml, subscribes to EventBus events, and dispatches
actions to registered handlers.  Provides REST API routes for rule management
and engine status.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError

from casectl.plugins.automation.engine import ActionRegistry, RulesEngine
from casectl.plugins.automation.models import (
    AutomationConfig,
    RuleAction,
)
from casectl.plugins.base import PluginContext, PluginStatus

logger = logging.getLogger(__name__)


def _build_default_action_handlers(ctx: PluginContext) -> dict[str, Any]:
    """Create built-in action handlers that delegate to event bus emissions.

    These are thin wrappers that translate rule actions into EventBus
    events, which the appropriate plugin then handles.
    """

    async def _handle_emit(action: RuleAction) -> None:
        """Emit an event on the bus (meta-action for chaining rules)."""
        event_name = action.params.get("event", "")
        event_data = action.params.get("data")
        if event_name:
            ctx.emit_event(event_name, event_data)

    async def _handle_log(action: RuleAction) -> None:
        """Log a message (useful for debugging rules)."""
        message = action.params.get("message", "")
        level = action.params.get("level", "info").lower()
        rule_logger = logging.getLogger("casectl.automation.rules")
        log_fn = getattr(rule_logger, level, rule_logger.info)
        log_fn("Rule action: %s", message)

    async def _handle_fan(action: RuleAction) -> None:
        """Dispatch fan control actions via event bus."""
        ctx.emit_event(f"automation.fan.{action.command}", action.params)

    async def _handle_led(action: RuleAction) -> None:
        """Dispatch LED control actions via event bus."""
        ctx.emit_event(f"automation.led.{action.command}", action.params)

    async def _handle_oled(action: RuleAction) -> None:
        """Dispatch OLED actions via event bus."""
        ctx.emit_event(f"automation.oled.{action.command}", action.params)

    async def _handle_alert(action: RuleAction) -> None:
        """Dispatch alert actions via event bus.

        The alerting plugin subscribes to ``automation.alert.*`` events
        and sends notifications via configured channels (webhook, ntfy, SMTP).
        """
        ctx.emit_event(f"automation.alert.{action.command}", {
            "target": action.target,
            "command": action.command,
            "params": action.params,
        })

    return {
        "emit": _handle_emit,
        "log": _handle_log,
        "fan": _handle_fan,
        "led": _handle_led,
        "oled": _handle_oled,
        "alert": _handle_alert,
    }


class AutomationPlugin:
    """Event-driven automation rules engine plugin.

    Conforms to the :class:`CasePlugin` protocol via structural subtyping.
    """

    name = "automation"
    version = "0.2.0"
    description = "Event-driven automation rules engine with priority conflict resolution"
    min_daemon_version = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._engine: RulesEngine | None = None
        self._action_registry: ActionRegistry = ActionRegistry()
        self._subscribed_events: set[str] = set()
        self._started = False

    @property
    def engine(self) -> RulesEngine | None:
        """The current rules engine instance, or ``None`` before start."""
        return self._engine

    @property
    def action_registry(self) -> ActionRegistry:
        """The action handler registry."""
        return self._action_registry

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes and prepare the engine (no event subscriptions yet)."""
        self._ctx = ctx

        # Register built-in action handlers
        handlers = _build_default_action_handlers(ctx)
        for target, handler in handlers.items():
            self._action_registry.register(target, handler)

        # Register API routes
        router = self._build_routes()
        ctx.register_routes(router)

        ctx.logger.info("Automation plugin set up with %d built-in action targets",
                        len(self._action_registry.targets))

    async def start(self) -> None:
        """Load rules from config and subscribe to events."""
        if self._ctx is None:
            logger.error("Automation plugin not set up — cannot start")
            return

        config = await self._load_config()
        self._engine = RulesEngine(config, self._action_registry)

        if config.enabled:
            self._subscribe_to_rule_events()
            self._started = True
            logger.info(
                "Automation engine started: %d rules, %d event subscriptions",
                len(config.rules),
                len(self._subscribed_events),
            )
        else:
            logger.info("Automation engine disabled in config")

    async def stop(self) -> None:
        """Unsubscribe from all events and shut down."""
        self._started = False
        self._subscribed_events.clear()
        logger.info("Automation engine stopped")

    def get_status(self) -> dict[str, Any]:
        """Return plugin health and diagnostics."""
        if self._engine is None:
            return {"status": PluginStatus.STOPPED}

        stats = self._engine.stats.to_dict()
        return {
            "status": PluginStatus.HEALTHY if self._started else PluginStatus.STOPPED,
            "enabled": self._engine.config.enabled,
            "rule_count": len(self._engine.rules),
            "subscribed_events": sorted(self._subscribed_events),
            "stats": stats,
        }

    # -- internal helpers ----------------------------------------------------

    async def _load_config(self) -> AutomationConfig:
        """Load automation config from the plugins section."""
        assert self._ctx is not None
        raw = await self._ctx.get_config()

        if not raw:
            return AutomationConfig()

        try:
            return AutomationConfig.model_validate(raw)
        except ValidationError as exc:
            logger.warning("Invalid automation config: %s — using defaults", exc)
            return AutomationConfig()

    def _subscribe_to_rule_events(self) -> None:
        """Subscribe to all unique events referenced by enabled rules."""
        if self._engine is None or self._ctx is None:
            return

        events = {r.event for r in self._engine.rules if r.enabled}
        for event in events:
            if event not in self._subscribed_events:
                self._ctx.on_event(event, self._make_event_handler(event))
                self._subscribed_events.add(event)

    def _make_event_handler(self, event: str) -> Any:
        """Create an async handler for a specific event name."""

        async def _handler(data: Any) -> None:
            if self._engine is not None:
                await self._engine.process_event(event, data)

        # Give the handler a meaningful name for logging
        _handler.__name__ = f"automation_handler_{event}"
        _handler.__qualname__ = f"AutomationPlugin._handler_{event}"
        return _handler

    async def reload_config(self) -> int:
        """Reload rules from config and re-subscribe to events.

        Returns the number of rules loaded.
        """
        config = await self._load_config()
        if self._engine is not None:
            self._engine.reload(config)
        else:
            self._engine = RulesEngine(config, self._action_registry)

        # Re-subscribe to any new events
        self._subscribe_to_rule_events()

        return len(config.rules)

    def _build_routes(self) -> APIRouter:
        """Create FastAPI routes for the automation API."""
        router = APIRouter(tags=["automation"])
        plugin = self  # capture for closures

        @router.get("/status")
        async def get_status() -> dict[str, Any]:
            """Return automation engine status and statistics."""
            return plugin.get_status()

        @router.get("/rules")
        async def list_rules() -> list[dict[str, Any]]:
            """List all configured automation rules."""
            if plugin._engine is None:
                return []
            return [r.model_dump(mode="json") for r in plugin._engine.rules]

        @router.get("/rules/{rule_name}")
        async def get_rule(rule_name: str) -> dict[str, Any]:
            """Get a specific rule by name."""
            if plugin._engine is None:
                return {"error": "Engine not started"}
            for rule in plugin._engine.rules:
                if rule.name == rule_name:
                    return rule.model_dump(mode="json")
            return {"error": f"Rule '{rule_name}' not found"}

        @router.post("/reload")
        async def reload_rules() -> dict[str, Any]:
            """Reload rules from config."""
            count = await plugin.reload_config()
            return {"reloaded": True, "rule_count": count}

        return router
