"""Alert delivery channels — webhook, ntfy.sh, and SMTP.

Each channel is a standalone async function that accepts a structured alert
payload and delivers it via its respective transport.  Channels are designed
to be composable: the :class:`AlertDispatcher` sends to all configured
channels in parallel.
"""

from __future__ import annotations

import asyncio
import email.message
import logging
import smtplib
from typing import Any

import httpx

from casectl.config.models import AlertConfig

logger = logging.getLogger(__name__)

# Timeout for HTTP requests (webhook and ntfy)
HTTP_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Alert payload
# ---------------------------------------------------------------------------


class AlertPayload:
    """Structured alert data passed to delivery channels.

    Attributes
    ----------
    title:
        Short alert title (e.g. "CPU Overheating").
    message:
        Detailed alert message body.
    severity:
        Severity level: ``info``, ``warning``, ``critical``.
    source:
        Origin of the alert (e.g. rule name or "threshold-monitor").
    extra:
        Arbitrary key-value pairs for channel-specific formatting.
    """

    __slots__ = ("title", "message", "severity", "source", "extra")

    def __init__(
        self,
        title: str,
        message: str,
        severity: str = "warning",
        source: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.title = title
        self.message = message
        self.severity = severity
        self.source = source
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON payloads."""
        return {
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "source": self.source,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Webhook channel
# ---------------------------------------------------------------------------


async def send_webhook(config: AlertConfig, payload: AlertPayload) -> bool:
    """Send an alert via HTTP webhook.

    Posts a JSON payload to ``config.webhook_url``.  Returns ``True`` on
    success (2xx response), ``False`` on failure.
    """
    if not config.webhook_url:
        return False

    headers = {"Content-Type": "application/json", **config.webhook_headers}
    method = config.webhook_method.upper()
    if method not in ("POST", "PUT"):
        method = "POST"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.request(
                method,
                config.webhook_url,
                json=payload.to_dict(),
                headers=headers,
            )
            if response.is_success:
                logger.debug("Webhook alert sent to %s — %d", config.webhook_url, response.status_code)
                return True
            logger.warning(
                "Webhook alert failed: %s returned %d",
                config.webhook_url,
                response.status_code,
            )
            return False
    except httpx.HTTPError as exc:
        logger.error("Webhook alert error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# ntfy.sh channel
# ---------------------------------------------------------------------------

_NTFY_SEVERITY_TO_PRIORITY: dict[str, str] = {
    "info": "3",
    "warning": "4",
    "critical": "5",
}


async def send_ntfy(config: AlertConfig, payload: AlertPayload) -> bool:
    """Send an alert via ntfy.sh push notification.

    Posts to ``{config.ntfy_url}/{config.ntfy_topic}`` with appropriate
    headers for title, priority, and tags.  Returns ``True`` on success.
    """
    if not config.ntfy_topic:
        return False

    url = f"{config.ntfy_url.rstrip('/')}/{config.ntfy_topic}"

    # Map severity to ntfy priority (use config override if set)
    priority = str(config.ntfy_priority)
    auto_priority = _NTFY_SEVERITY_TO_PRIORITY.get(payload.severity)
    if auto_priority and int(auto_priority) > config.ntfy_priority:
        priority = auto_priority

    headers: dict[str, str] = {
        "Title": payload.title,
        "Priority": priority,
        "Tags": f"casectl,{payload.severity}",
    }

    if config.ntfy_token:
        headers["Authorization"] = f"Bearer {config.ntfy_token}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(
                url,
                content=payload.message,
                headers=headers,
            )
            if response.is_success:
                logger.debug("ntfy alert sent to %s — %d", url, response.status_code)
                return True
            logger.warning("ntfy alert failed: %s returned %d", url, response.status_code)
            return False
    except httpx.HTTPError as exc:
        logger.error("ntfy alert error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# SMTP channel
# ---------------------------------------------------------------------------


async def send_smtp(config: AlertConfig, payload: AlertPayload) -> bool:
    """Send an alert via SMTP email.

    Connects to ``config.smtp_host:config.smtp_port`` using STARTTLS,
    authenticates with ``smtp_user``/``smtp_password``, and sends a
    plain-text email.  Returns ``True`` on success.

    Runs the blocking SMTP operations in a thread pool to avoid blocking
    the event loop.
    """
    if not config.smtp_host or not config.smtp_to:
        return False

    msg = email.message.EmailMessage()
    msg["Subject"] = f"{config.smtp_subject_prefix} {payload.title}"
    msg["From"] = config.smtp_user or f"casectl@{config.smtp_host}"
    msg["To"] = config.smtp_to
    msg.set_content(
        f"Severity: {payload.severity}\n"
        f"Source: {payload.source}\n\n"
        f"{payload.message}"
    )

    def _send() -> bool:
        try:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=HTTP_TIMEOUT) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if config.smtp_user and config.smtp_password:
                    server.login(config.smtp_user, config.smtp_password)
                server.send_message(msg)
            logger.debug("SMTP alert sent to %s via %s", config.smtp_to, config.smtp_host)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("SMTP alert error: %s", exc)
            return False

    return await asyncio.to_thread(_send)


# ---------------------------------------------------------------------------
# Dispatcher — sends to all configured channels
# ---------------------------------------------------------------------------


class AlertDispatcher:
    """Sends alerts to all configured channels in parallel.

    Tracks per-alert-type cooldowns to prevent notification spam.
    """

    def __init__(self, config: AlertConfig) -> None:
        self._config = config
        self._stats = AlertStats()

    @property
    def config(self) -> AlertConfig:
        """Current alert configuration."""
        return self._config

    @config.setter
    def config(self, value: AlertConfig) -> None:
        self._config = value

    @property
    def stats(self) -> AlertStats:
        """Delivery statistics."""
        return self._stats

    async def dispatch(self, payload: AlertPayload) -> dict[str, bool]:
        """Send *payload* to all configured channels in parallel.

        Returns a dict mapping channel name to delivery success.
        """
        if not self._config.enabled:
            logger.debug("Alerting disabled — skipping dispatch")
            return {}

        results: dict[str, bool] = {}

        # Build list of channel coroutines for channels that are configured
        tasks: list[tuple[str, Any]] = []
        if self._config.webhook_url:
            tasks.append(("webhook", send_webhook(self._config, payload)))
        if self._config.ntfy_topic:
            tasks.append(("ntfy", send_ntfy(self._config, payload)))
        if self._config.smtp_host and self._config.smtp_to:
            tasks.append(("smtp", send_smtp(self._config, payload)))

        if not tasks:
            logger.debug("No alert channels configured — skipping dispatch")
            return results

        # Execute all channels in parallel
        channel_names = [name for name, _ in tasks]
        coros = [coro for _, coro in tasks]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        for name, outcome in zip(channel_names, outcomes):
            if isinstance(outcome, Exception):
                logger.error("Alert channel '%s' raised: %s", name, outcome)
                results[name] = False
                self._stats.failures += 1
            elif outcome:
                results[name] = True
                self._stats.delivered += 1
            else:
                results[name] = False
                self._stats.failures += 1

        self._stats.total_dispatched += 1
        return results


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


class AlertStats:
    """Lightweight counters for alert delivery diagnostics."""

    def __init__(self) -> None:
        self.total_dispatched: int = 0
        self.delivered: int = 0
        self.failures: int = 0
        self.skipped_cooldown: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return stats as a plain dict."""
        return {
            "total_dispatched": self.total_dispatched,
            "delivered": self.delivered,
            "failures": self.failures,
            "skipped_cooldown": self.skipped_cooldown,
        }
