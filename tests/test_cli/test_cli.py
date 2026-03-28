"""Tests for casectl.cli.main — CLI commands using Click's CliRunner.

All tests use CliRunner in isolation mode, so no real HTTP requests
or daemon connections are made.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from casectl.cli.main import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner() -> CliRunner:
    """Return a Click CliRunner with isolated environment."""
    return CliRunner()


def _mock_api_get(response_data: dict[str, Any]) -> MagicMock:
    """Create a mock httpx.Client that returns *response_data* for any GET."""
    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_response
    mock_client.post.return_value = mock_response
    mock_client.patch.return_value = mock_response
    return mock_client


# ---------------------------------------------------------------------------
# doctor command
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    """Verify `casectl doctor` runs local checks without crashing."""

    def test_doctor_runs_without_error(self) -> None:
        """Doctor should exit 0 even if checks fail (it prints failures)."""
        runner = _runner()
        result = runner.invoke(cli, ["doctor"])
        # Doctor should never crash — it reports failures in a table
        assert result.exit_code == 0
        assert "casectl doctor" in result.output

    def test_doctor_contains_check_labels(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["doctor"])
        assert "Python >= 3.11" in result.output

    def test_doctor_shows_pass_or_fail(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["doctor"])
        # At minimum Python check should pass
        assert "PASS" in result.output or "FAIL" in result.output

    def test_doctor_checks_count(self) -> None:
        """Doctor should report a total number of checks."""
        runner = _runner()
        result = runner.invoke(cli, ["doctor"])
        assert "checks" in result.output.lower()

    def test_doctor_no_daemon_needed(self) -> None:
        """Doctor runs purely locally — no API call should be made."""
        runner = _runner()
        with patch("casectl.cli.main.httpx") as mock_httpx:
            result = runner.invoke(cli, ["doctor"])
        # httpx.Client should never be instantiated for doctor
        mock_httpx.Client.assert_not_called()


# ---------------------------------------------------------------------------
# Default status (no subcommand)
# ---------------------------------------------------------------------------


class TestDefaultStatus:
    """Verify `casectl` with no args prints a status summary."""

    def test_default_status_calls_health_api(self) -> None:
        mock_client = _mock_api_get({
            "status": "healthy",
            "version": "0.1.0",
            "uptime": 3661,
            "plugins": [
                {"name": "fan-control", "status": "running"},
            ],
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, [])

        assert result.exit_code == 0
        assert "casectl status" in result.output

    def test_default_status_shows_version(self) -> None:
        mock_client = _mock_api_get({
            "status": "healthy",
            "version": "0.1.0",
            "uptime": 0,
            "plugins": [],
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, [])

        assert "0.1.0" in result.output

    def test_default_status_shows_uptime(self) -> None:
        mock_client = _mock_api_get({
            "status": "healthy",
            "version": "0.1.0",
            "uptime": 7200,  # 2 hours
            "plugins": [],
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, [])

        assert "2" in result.output  # hours

    def test_default_status_shows_plugins(self) -> None:
        mock_client = _mock_api_get({
            "status": "healthy",
            "version": "0.1.0",
            "uptime": 0,
            "plugins": [
                {"name": "fan-control", "status": "running"},
                {"name": "led-control", "status": "running"},
            ],
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, [])

        assert "fan-control" in result.output
        assert "led-control" in result.output


# ---------------------------------------------------------------------------
# serve --help
# ---------------------------------------------------------------------------


class TestServeHelp:
    """Verify `casectl serve --help` prints help text."""

    def test_serve_help(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "Start the casectl daemon" in result.output

    def test_serve_help_shows_options(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["serve", "--help"])
        assert "--bind" in result.output
        assert "--port" in result.output
        assert "--log-level" in result.output


# ---------------------------------------------------------------------------
# fan status without daemon
# ---------------------------------------------------------------------------


class TestFanStatusNoDaemon:
    """Verify `casectl fan status` handles ConnectError gracefully."""

    def test_fan_status_connect_error(self) -> None:
        """When daemon is not running, should print error and exit 1."""
        import httpx

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["fan", "status"])

        assert result.exit_code == 1
        assert "Cannot connect" in result.output

    def test_fan_status_success(self) -> None:
        mock_client = _mock_api_get({
            "mode": "follow_temp",
            "degraded": False,
            "temp": 42.5,
            "duty": [100, 100, 100],
            "rpm": [1200, 1200, 1200],
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["fan", "status"])

        assert result.exit_code == 0
        assert "Fan Status" in result.output


# ---------------------------------------------------------------------------
# LED commands
# ---------------------------------------------------------------------------


class TestLedCommands:
    """Verify LED CLI commands."""

    def test_led_status(self) -> None:
        mock_client = _mock_api_get({
            "mode": "rainbow",
            "color": {"red": 0, "green": 0, "blue": 255},
            "degraded": False,
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["led", "status"])

        assert result.exit_code == 0
        assert "LED Status" in result.output

    def test_led_mode_command(self) -> None:
        mock_client = _mock_api_get({"mode": "manual"})

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["led", "mode", "manual"])

        assert result.exit_code == 0
        assert "LED mode set to" in result.output

    def test_led_color_command(self) -> None:
        mock_client = _mock_api_get({"color": {"red": 255, "green": 0, "blue": 128}})

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["led", "color", "255", "0", "128"])

        assert result.exit_code == 0
        assert "LED colour set to" in result.output

    def test_led_color_out_of_range(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["led", "color", "256", "0", "0"])
        assert result.exit_code == 2  # Click UsageError for IntRange violation


# ---------------------------------------------------------------------------
# Monitor command
# ---------------------------------------------------------------------------


class TestMonitorCommand:
    """Verify `casectl monitor` prints system metrics."""

    def test_monitor_success(self) -> None:
        mock_client = _mock_api_get({
            "cpu_percent": 25.3,
            "memory_percent": 45.0,
            "disk_percent": 60.0,
            "cpu_temp": 42.0,
            "case_temp": 35.0,
            "ip_address": "192.168.1.100",
            "fan_duty": [100, 100, 100],
            "motor_speed": [1200, 1200, 1200],
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["monitor"])

        assert result.exit_code == 0
        assert "System Monitor" in result.output


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


class TestConfigCommands:
    """Verify config CLI commands."""

    def test_config_get(self) -> None:
        mock_client = _mock_api_get({
            "mode": 0,
            "manual_duty": [75, 75, 75],
        })

        runner = _runner()
        with patch("casectl.cli.main.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["config", "get", "fan"])

        assert result.exit_code == 0
        assert "Config: fan" in result.output


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestHelpText:
    """Verify help text for various commands."""

    def test_root_help(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "casectl" in result.output

    def test_fan_help(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["fan", "--help"])
        assert result.exit_code == 0
        assert "Fan control commands" in result.output

    def test_led_help(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["led", "--help"])
        assert result.exit_code == 0

    def test_oled_help(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["oled", "--help"])
        assert result.exit_code == 0

    def test_service_help(self) -> None:
        runner = _runner()
        result = runner.invoke(cli, ["service", "--help"])
        assert result.exit_code == 0
        assert "Manage the casectl systemd service" in result.output
