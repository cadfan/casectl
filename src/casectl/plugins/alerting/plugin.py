"""Alerting plugin — webhook, ntfy.sh, and SMTP notification channels.

Integrates with the automation rules engine by registering an ``alert``
action handler.  Automation rules can trigger alerts like::

    actions:
      - target: alert
        command: send
        params:
          title: "CPU Overheating"
          message: "Temperature is {{ cpu_temp }}°C"
          severity: critical

Also monitors threshold breaches from ``metrics_updated`` events when
enabled in config.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter

from casectl.config.models import AlertConfig
from casectl.plugins.alerting.channels import AlertDispatcher, AlertPayload
from casectl.plugins.base import PluginContext, PluginStatus

logger = logging.getLogger(__name__)


class AlertingPlugin:
    """Alerting plugin with webhook, ntfy.sh, and SMTP channels.

    Conforms to the :class:`CasePlugin` protocol via structural subtyping.

    Integration with automation
    --------------------------
    During ``setup()``, the plugin looks for the automation plugin's
    :class:`ActionRegistry` in ``app_state`` and registers an ``alert``
    handler.  This allows automation rules to use ``target: "alert"``
    actions to send notifications via any configured channel.

    The alert action handler accepts these ``params``:

    * ``title`` (str) — Alert title.
    * ``message`` (str) — Alert body text.
    * ``severity`` (str) — One of ``info``, ``warning``, ``critical``.
    * ``channels`` (list[str], optional) — Restrict to specific channels.
    """

    name = "alerting"
    version = "0.2.0"
    description = "Webhook, ntfy.sh, and SMTP alerting with automation integration"
    min_daemon_version = "0.1.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._dispatcher: AlertDispatcher | None = None
        self._config: AlertConfig = AlertConfig()
        self._started = False
        # Per-type cooldown tracking: alert_key → last_fired_timestamp
        self._cooldowns: dict[str, float] = {}

    @property
    def dispatcher(self) -> AlertDispatcher | None:
        """The alert dispatcher instance, or ``None`` before start."""
        return self._dispatcher

    async def setup(self, ctx: PluginContext) -> None:
        """Register routes and the ``alert`` action handler for automation."""
        self._ctx = ctx

        # Register API routes
        router = self._build_routes()
        ctx.register_routes(router)

        ctx.logger.info("Alerting plugin set up")

    def register_alert_handler(self, action_registry: Any) -> None:
        """Register the ``alert`` action handler on an automation ActionRegistry.

        This is called by the plugin host or the automation plugin to wire up
        the integration.  It can also be called manually in tests.

        Parameters
        ----------
        action_registry:
            The :class:`ActionRegistry` from the automation engine.
        """
        action_registry.register("alert", self._handle_alert_action)
        logger.info("Registered 'alert' action handler on automation engine")

    async def start(self) -> None:
        """Load config and initialise the dispatcher."""
        if self._ctx is None:
            logger.error("Alerting plugin not set up — cannot start")
            return

        self._config = await self._load_config()
        self._dispatcher = AlertDispatcher(self._config)
        self._started = True

        channels = []
        if self._config.webhook_url:
            channels.append("webhook")
        if self._config.ntfy_topic:
            channels.append("ntfy")
        if self._config.smtp_host and self._config.smtp_to:
            channels.append("smtp")

        logger.info(
            "Alerting plugin started: enabled=%s, channels=%s",
            self._config.enabled,
            channels or ["none"],
        )

    async def stop(self) -> None:
        """Shut down the alerting plugin."""
        self._started = False
        logger.info("Alerting plugin stopped")

    def get_status(self) -> dict[str, Any]:
        """Return plugin health and diagnostics."""
        if not self._started or self._dispatcher is None:
            return {"status": PluginStatus.STOPPED}

        channels = []
        if self._config.webhook_url:
            channels.append("webhook")
        if self._config.ntfy_topic:
            channels.append("ntfy")
        if self._config.smtp_host and self._config.smtp_to:
            channels.append("smtp")

        return {
            "status": PluginStatus.HEALTHY if self._config.enabled else PluginStatus.STOPPED,
            "enabled": self._config.enabled,
            "channels": channels,
            "stats": self._dispatcher.stats.to_dict(),
        }

    # -- automation action handler -------------------------------------------

    async def _handle_alert_action(self, action: Any) -> None:
        """Handle an ``alert`` action from the automation rules engine.

        Expected ``action.params``:

        * ``title`` (str) — Alert title.
        * ``message`` (str) — Alert body.
        * ``severity`` (str) — ``info`` / ``warning`` / ``critical``.
        * ``source`` (str, optional) — Source identifier.

        The ``action.command`` can be:

        * ``send`` — Send to all configured channels.
        * ``webhook`` — Send only via webhook.
        * ``ntfy`` — Send only via ntfy.sh.
        * ``smtp`` — Send only via SMTP.
        """
        if self._dispatcher is None:
            logger.warning("Alert action received but dispatcher not initialised")
            return

        params = action.params
        title = params.get("title", "casectl Alert")
        message = params.get("message", "")
        severity = params.get("severity", "warning")
        source = params.get("source", f"rule:{action.command}")

        # Cooldown check: use title as cooldown key
        cooldown_key = f"{title}:{severity}"
        if not self._check_cooldown(cooldown_key):
            logger.debug("Alert '%s' skipped — in cooldown", cooldown_key)
            if self._dispatcher:
                self._dispatcher.stats.skipped_cooldown += 1
            return

        payload = AlertPayload(
            title=title,
            message=message,
            severity=severity,
            source=source,
            extra={k: v for k, v in params.items()
                   if k not in ("title", "message", "severity", "source")},
        )

        # Route to specific channel if command specifies one
        command = action.command.lower()
        if command in ("webhook", "ntfy", "smtp"):
            await self._send_single_channel(command, payload)
        else:
            # Default: send to all configured channels
            await self._dispatcher.dispatch(payload)

        self._record_cooldown(cooldown_key)

    async def _send_single_channel(self, channel: str, payload: AlertPayload) -> bool:
        """Send an alert to a single named channel."""
        from casectl.plugins.alerting.channels import send_ntfy, send_smtp, send_webhook

        if channel == "webhook":
            return await send_webhook(self._config, payload)
        if channel == "ntfy":
            return await send_ntfy(self._config, payload)
        if channel == "smtp":
            return await send_smtp(self._config, payload)
        logger.warning("Unknown alert channel: %s", channel)
        return False

    # -- cooldown management -------------------------------------------------

    def _check_cooldown(self, key: str) -> bool:
        """Return ``True`` if *key* is NOT in cooldown."""
        if self._config.cooldown <= 0:
            return True
        last_fired = self._cooldowns.get(key, 0.0)
        return (time.monotonic() - last_fired) >= self._config.cooldown

    def _record_cooldown(self, key: str) -> None:
        """Record the current time as the last firing time for *key*."""
        if self._config.cooldown > 0:
            self._cooldowns[key] = time.monotonic()

    # -- config loading ------------------------------------------------------

    async def _load_config(self) -> AlertConfig:
        """Load alert config from the daemon config."""
        if self._ctx is None:
            return AlertConfig()

        # Alerts config lives at the top level, not under plugins
        if self._ctx.config_manager is None:
            return AlertConfig()

        try:
            raw = await self._ctx.config_manager.get("alerts")
            if raw is None:
                return AlertConfig()
            if isinstance(raw, AlertConfig):
                return raw
            if isinstance(raw, dict):
                return AlertConfig.model_validate(raw)
            return AlertConfig()
        except Exception as exc:
            logger.warning("Invalid alert config: %s — using defaults", exc)
            return AlertConfig()

    # -- REST API routes -----------------------------------------------------

    def _build_routes(self) -> APIRouter:
        """Create FastAPI routes for the alerting API."""
        router = APIRouter(tags=["alerting"])
        plugin = self

        @router.get("/status")
        async def get_status() -> dict[str, Any]:
            """Return alerting plugin status and statistics."""
            return plugin.get_status()

        @router.post("/test")
        async def send_test_alert(
            title: str = "Test Alert",
            message: str = "This is a test alert from casectl.",
            severity: str = "info",
        ) -> dict[str, Any]:
            """Send a test alert through all configured channels."""
            if plugin._dispatcher is None:
                return {"error": "Alerting plugin not started"}
            payload = AlertPayload(
                title=title,
                message=message,
                severity=severity,
                source="test",
            )
            results = await plugin._dispatcher.dispatch(payload)
            return {"sent": True, "channels": results}

        return router
