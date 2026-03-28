"""Tests for alerting delivery channels — webhook, ntfy.sh, SMTP."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.config.models import AlertConfig
from casectl.plugins.alerting.channels import (
    AlertDispatcher,
    AlertPayload,
    AlertStats,
    send_ntfy,
    send_smtp,
    send_webhook,
)


# ---------------------------------------------------------------------------
# AlertPayload tests
# ---------------------------------------------------------------------------


class TestAlertPayload:
    """Test AlertPayload construction and serialisation."""

    def test_basic_construction(self) -> None:
        p = AlertPayload(title="Test", message="Hello")
        assert p.title == "Test"
        assert p.message == "Hello"
        assert p.severity == "warning"
        assert p.source == ""
        assert p.extra == {}

    def test_full_construction(self) -> None:
        p = AlertPayload(
            title="CPU Hot",
            message="85°C",
            severity="critical",
            source="overheat-rule",
            extra={"cpu_temp": 85.0},
        )
        assert p.severity == "critical"
        assert p.source == "overheat-rule"
        assert p.extra["cpu_temp"] == 85.0

    def test_to_dict(self) -> None:
        p = AlertPayload(
            title="Test",
            message="Body",
            severity="info",
            source="test",
            extra={"key": "value"},
        )
        d = p.to_dict()
        assert d["title"] == "Test"
        assert d["message"] == "Body"
        assert d["severity"] == "info"
        assert d["source"] == "test"
        assert d["key"] == "value"

    def test_to_dict_no_extra(self) -> None:
        p = AlertPayload(title="T", message="M")
        d = p.to_dict()
        assert "title" in d
        assert "message" in d


# ---------------------------------------------------------------------------
# Webhook channel tests
# ---------------------------------------------------------------------------


class TestSendWebhook:
    """Test webhook delivery channel."""

    async def test_no_url_returns_false(self) -> None:
        config = AlertConfig(webhook_url="")
        payload = AlertPayload(title="T", message="M")
        assert await send_webhook(config, payload) is False

    async def test_successful_post(self) -> None:
        config = AlertConfig(webhook_url="https://example.com/hook")
        payload = AlertPayload(title="Test", message="Body")

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            result = await send_webhook(config, payload)
            assert result is True
            mock_client.request.assert_called_once()
            call_args = mock_client.request.call_args
            assert call_args[0][0] == "POST"
            assert call_args[0][1] == "https://example.com/hook"

    async def test_failed_response(self) -> None:
        config = AlertConfig(webhook_url="https://example.com/hook")
        payload = AlertPayload(title="T", message="M")

        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            result = await send_webhook(config, payload)
            assert result is False

    async def test_http_error(self) -> None:
        import httpx

        config = AlertConfig(webhook_url="https://example.com/hook")
        payload = AlertPayload(title="T", message="M")

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.ConnectError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            result = await send_webhook(config, payload)
            assert result is False

    async def test_custom_method_and_headers(self) -> None:
        config = AlertConfig(
            webhook_url="https://example.com/hook",
            webhook_method="PUT",
            webhook_headers={"X-Custom": "value"},
        )
        payload = AlertPayload(title="T", message="M")

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            await send_webhook(config, payload)
            call_args = mock_client.request.call_args
            assert call_args[0][0] == "PUT"
            headers = call_args[1]["headers"]
            assert headers["X-Custom"] == "value"

    async def test_invalid_method_defaults_to_post(self) -> None:
        config = AlertConfig(
            webhook_url="https://example.com/hook",
            webhook_method="DELETE",
        )
        payload = AlertPayload(title="T", message="M")

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            await send_webhook(config, payload)
            assert mock_client.request.call_args[0][0] == "POST"


# ---------------------------------------------------------------------------
# ntfy.sh channel tests
# ---------------------------------------------------------------------------


class TestSendNtfy:
    """Test ntfy.sh delivery channel."""

    async def test_no_topic_returns_false(self) -> None:
        config = AlertConfig(ntfy_topic="")
        payload = AlertPayload(title="T", message="M")
        assert await send_ntfy(config, payload) is False

    async def test_successful_send(self) -> None:
        config = AlertConfig(ntfy_topic="casectl-alerts")
        payload = AlertPayload(title="CPU Hot", message="85°C", severity="critical")

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            result = await send_ntfy(config, payload)
            assert result is True
            call_args = mock_client.post.call_args
            assert "casectl-alerts" in call_args[0][0]
            assert call_args[1]["content"] == "85°C"
            headers = call_args[1]["headers"]
            assert headers["Title"] == "CPU Hot"

    async def test_severity_escalates_priority(self) -> None:
        config = AlertConfig(ntfy_topic="alerts", ntfy_priority=3)
        payload = AlertPayload(title="T", message="M", severity="critical")

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            await send_ntfy(config, payload)
            headers = mock_client.post.call_args[1]["headers"]
            # critical → priority 5, which is > config default of 3
            assert headers["Priority"] == "5"

    async def test_auth_token_sent(self) -> None:
        config = AlertConfig(ntfy_topic="alerts", ntfy_token="tk_secret")
        payload = AlertPayload(title="T", message="M")

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            await send_ntfy(config, payload)
            headers = mock_client.post.call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer tk_secret"

    async def test_custom_server_url(self) -> None:
        config = AlertConfig(
            ntfy_url="https://ntfy.example.com/",
            ntfy_topic="my-alerts",
        )
        payload = AlertPayload(title="T", message="M")

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            await send_ntfy(config, payload)
            url = mock_client.post.call_args[0][0]
            assert url == "https://ntfy.example.com/my-alerts"

    async def test_failed_response(self) -> None:
        config = AlertConfig(ntfy_topic="alerts")
        payload = AlertPayload(title="T", message="M")

        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 403

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            result = await send_ntfy(config, payload)
            assert result is False

    async def test_http_error(self) -> None:
        import httpx

        config = AlertConfig(ntfy_topic="alerts")
        payload = AlertPayload(title="T", message="M")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.httpx.AsyncClient", return_value=mock_client):
            result = await send_ntfy(config, payload)
            assert result is False


# ---------------------------------------------------------------------------
# SMTP channel tests
# ---------------------------------------------------------------------------


class TestSendSmtp:
    """Test SMTP delivery channel."""

    async def test_no_host_returns_false(self) -> None:
        config = AlertConfig(smtp_host="", smtp_to="test@example.com")
        payload = AlertPayload(title="T", message="M")
        assert await send_smtp(config, payload) is False

    async def test_no_recipient_returns_false(self) -> None:
        config = AlertConfig(smtp_host="mail.example.com", smtp_to="")
        payload = AlertPayload(title="T", message="M")
        assert await send_smtp(config, payload) is False

    async def test_successful_send(self) -> None:
        config = AlertConfig(
            smtp_host="mail.example.com",
            smtp_port=587,
            smtp_user="alerts@example.com",
            smtp_password="secret",
            smtp_to="admin@example.com",
            smtp_subject_prefix="[casectl]",
        )
        payload = AlertPayload(title="CPU Hot", message="85°C", severity="critical")

        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.smtplib.SMTP", return_value=mock_smtp):
            result = await send_smtp(config, payload)
            assert result is True
            mock_smtp.ehlo.assert_called()
            mock_smtp.starttls.assert_called_once()
            mock_smtp.login.assert_called_once_with("alerts@example.com", "secret")
            mock_smtp.send_message.assert_called_once()

    async def test_smtp_error(self) -> None:
        import smtplib

        config = AlertConfig(
            smtp_host="mail.example.com",
            smtp_to="admin@example.com",
        )
        payload = AlertPayload(title="T", message="M")

        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)
        mock_smtp.ehlo = MagicMock(side_effect=smtplib.SMTPException("Connection refused"))

        with patch("casectl.plugins.alerting.channels.smtplib.SMTP", return_value=mock_smtp):
            result = await send_smtp(config, payload)
            assert result is False

    async def test_no_auth_when_no_user(self) -> None:
        config = AlertConfig(
            smtp_host="mail.example.com",
            smtp_to="admin@example.com",
            smtp_user="",
            smtp_password="",
        )
        payload = AlertPayload(title="T", message="M")

        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("casectl.plugins.alerting.channels.smtplib.SMTP", return_value=mock_smtp):
            result = await send_smtp(config, payload)
            assert result is True
            mock_smtp.login.assert_not_called()


# ---------------------------------------------------------------------------
# AlertDispatcher tests
# ---------------------------------------------------------------------------


class TestAlertDispatcher:
    """Test the multi-channel alert dispatcher."""

    async def test_disabled_config_returns_empty(self) -> None:
        config = AlertConfig(enabled=False)
        dispatcher = AlertDispatcher(config)
        payload = AlertPayload(title="T", message="M")
        result = await dispatcher.dispatch(payload)
        assert result == {}

    async def test_no_channels_configured(self) -> None:
        config = AlertConfig(enabled=True)
        dispatcher = AlertDispatcher(config)
        payload = AlertPayload(title="T", message="M")
        result = await dispatcher.dispatch(payload)
        assert result == {}

    async def test_dispatch_to_webhook_only(self) -> None:
        config = AlertConfig(enabled=True, webhook_url="https://example.com/hook")
        dispatcher = AlertDispatcher(config)
        payload = AlertPayload(title="T", message="M")

        with patch("casectl.plugins.alerting.channels.send_webhook", new_callable=AsyncMock) as mock:
            mock.return_value = True
            result = await dispatcher.dispatch(payload)
            assert result["webhook"] is True
            assert dispatcher.stats.delivered == 1
            assert dispatcher.stats.total_dispatched == 1

    async def test_dispatch_to_multiple_channels(self) -> None:
        config = AlertConfig(
            enabled=True,
            webhook_url="https://example.com/hook",
            ntfy_topic="alerts",
        )
        dispatcher = AlertDispatcher(config)
        payload = AlertPayload(title="T", message="M")

        with (
            patch("casectl.plugins.alerting.channels.send_webhook", new_callable=AsyncMock) as wh,
            patch("casectl.plugins.alerting.channels.send_ntfy", new_callable=AsyncMock) as ntfy,
        ):
            wh.return_value = True
            ntfy.return_value = True
            result = await dispatcher.dispatch(payload)
            assert result["webhook"] is True
            assert result["ntfy"] is True
            assert dispatcher.stats.delivered == 2

    async def test_dispatch_handles_channel_exception(self) -> None:
        config = AlertConfig(enabled=True, webhook_url="https://example.com/hook")
        dispatcher = AlertDispatcher(config)
        payload = AlertPayload(title="T", message="M")

        with patch("casectl.plugins.alerting.channels.send_webhook", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("boom")
            result = await dispatcher.dispatch(payload)
            assert result["webhook"] is False
            assert dispatcher.stats.failures == 1

    async def test_dispatch_tracks_failures(self) -> None:
        config = AlertConfig(enabled=True, webhook_url="https://example.com/hook")
        dispatcher = AlertDispatcher(config)
        payload = AlertPayload(title="T", message="M")

        with patch("casectl.plugins.alerting.channels.send_webhook", new_callable=AsyncMock) as mock:
            mock.return_value = False
            result = await dispatcher.dispatch(payload)
            assert result["webhook"] is False
            assert dispatcher.stats.failures == 1

    def test_config_property(self) -> None:
        config = AlertConfig(enabled=True)
        dispatcher = AlertDispatcher(config)
        assert dispatcher.config is config

        new_config = AlertConfig(enabled=False)
        dispatcher.config = new_config
        assert dispatcher.config is new_config


# ---------------------------------------------------------------------------
# AlertStats tests
# ---------------------------------------------------------------------------


class TestAlertStats:
    """Test AlertStats counters and serialisation."""

    def test_defaults(self) -> None:
        s = AlertStats()
        assert s.total_dispatched == 0
        assert s.delivered == 0
        assert s.failures == 0
        assert s.skipped_cooldown == 0

    def test_to_dict(self) -> None:
        s = AlertStats()
        s.total_dispatched = 5
        s.delivered = 3
        s.failures = 2
        d = s.to_dict()
        assert d["total_dispatched"] == 5
        assert d["delivered"] == 3
        assert d["failures"] == 2
        assert d["skipped_cooldown"] == 0
