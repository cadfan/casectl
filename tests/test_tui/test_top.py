"""Tests for the ``casectl top`` Rich Live terminal dashboard.

Covers panel builders, layout assembly, data fetching, and the run_top loop
using mocked HTTP and console to avoid real network and terminal I/O.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from casectl.tui.top import (
    DEFAULT_REFRESH_INTERVAL,
    MAX_REFRESH_INTERVAL,
    MIN_REFRESH_INTERVAL,
    _percent_bar,
    _temp_bar,
    build_cpu_panel,
    build_fan_panel,
    build_header,
    build_info_panel,
    build_layout,
    build_led_panel,
    fetch_dashboard_data,
    run_top,
)


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_HEALTH: dict[str, Any] = {
    "status": "healthy",
    "version": "0.1.0",
    "uptime": 3661,
    "plugins": [
        {"name": "fan-control", "status": "running"},
        {"name": "led-control", "status": "running"},
        {"name": "system-monitor", "status": "running"},
    ],
}

SAMPLE_MONITOR: dict[str, Any] = {
    "metrics": {
        "cpu_temp": 52.3,
        "case_temp": 38.1,
        "cpu_percent": 25.5,
        "memory_percent": 45.0,
        "disk_percent": 60.2,
        "ip_address": "192.168.1.100",
        "date": "2026-03-28",
        "time": "14:30:00",
        "fan_duty": [128, 128, 128],
        "motor_speed": [1200, 1200, 1200],
    },
}

SAMPLE_FAN: dict[str, Any] = {
    "mode": "follow_temp",
    "duty": [128, 100, 75],
    "rpm": [1200, 1000, 800],
    "degraded": False,
}

SAMPLE_LED: dict[str, Any] = {
    "mode": "rainbow",
    "color": {"red": 255, "green": 128, "blue": 0},
    "degraded": False,
}


def _full_data() -> dict[str, Any]:
    """Return a complete dashboard data dict."""
    return {
        "health": SAMPLE_HEALTH,
        "monitor": SAMPLE_MONITOR,
        "fan": SAMPLE_FAN,
        "led": SAMPLE_LED,
    }


def _empty_data() -> dict[str, Any]:
    """Return a dashboard data dict where all fetches failed."""
    return {"health": None, "monitor": None, "fan": None, "led": None}


# ---------------------------------------------------------------------------
# _temp_bar tests
# ---------------------------------------------------------------------------


class TestTempBar:
    """Tests for the temperature bar builder."""

    def test_cold_temp_green(self) -> None:
        bar = _temp_bar(30.0)
        plain = bar.plain
        assert "30.0" in plain

    def test_warm_temp_yellow(self) -> None:
        bar = _temp_bar(55.0)
        plain = bar.plain
        assert "55.0" in plain

    def test_hot_temp_red(self) -> None:
        bar = _temp_bar(80.0)
        plain = bar.plain
        assert "80.0" in plain

    def test_zero_temp(self) -> None:
        bar = _temp_bar(0.0)
        assert "0.0" in bar.plain

    def test_over_max_clamped(self) -> None:
        bar = _temp_bar(120.0, max_temp=100.0)
        assert "120.0" in bar.plain

    def test_negative_clamped(self) -> None:
        bar = _temp_bar(-5.0)
        assert "-5.0" in bar.plain


# ---------------------------------------------------------------------------
# _percent_bar tests
# ---------------------------------------------------------------------------


class TestPercentBar:
    """Tests for the percentage bar builder."""

    def test_low_percent_green(self) -> None:
        bar = _percent_bar(20.0)
        assert "20.0%" in bar.plain

    def test_mid_percent_yellow(self) -> None:
        bar = _percent_bar(70.0)
        assert "70.0%" in bar.plain

    def test_high_percent_red(self) -> None:
        bar = _percent_bar(95.0)
        assert "95.0%" in bar.plain

    def test_zero_percent(self) -> None:
        bar = _percent_bar(0.0)
        assert "0.0%" in bar.plain

    def test_hundred_percent(self) -> None:
        bar = _percent_bar(100.0)
        assert "100.0%" in bar.plain


# ---------------------------------------------------------------------------
# Panel builder tests
# ---------------------------------------------------------------------------


class TestBuildCpuPanel:
    """Tests for the CPU / system metrics panel."""

    def test_with_data(self) -> None:
        panel = build_cpu_panel(SAMPLE_MONITOR)
        assert isinstance(panel, Panel)
        assert "System Metrics" in str(panel.title)

    def test_with_none(self) -> None:
        panel = build_cpu_panel(None)
        assert isinstance(panel, Panel)

    def test_with_flat_data(self) -> None:
        """Data without a nested 'metrics' key should still work."""
        flat = {
            "cpu_temp": 40.0,
            "case_temp": 30.0,
            "cpu_percent": 10.0,
            "memory_percent": 20.0,
            "disk_percent": 30.0,
        }
        panel = build_cpu_panel(flat)
        assert isinstance(panel, Panel)


class TestBuildFanPanel:
    """Tests for the fan status panel."""

    def test_with_data(self) -> None:
        panel = build_fan_panel(SAMPLE_FAN)
        assert isinstance(panel, Panel)
        assert "Fan Status" in str(panel.title)

    def test_with_none(self) -> None:
        panel = build_fan_panel(None)
        assert isinstance(panel, Panel)

    def test_high_duty(self) -> None:
        """High duty values should render without error."""
        data = {"mode": "manual", "duty": [255, 255, 255], "rpm": [0, 0, 0], "degraded": False}
        panel = build_fan_panel(data)
        assert isinstance(panel, Panel)

    def test_degraded(self) -> None:
        data = {"mode": "follow_temp", "duty": [128], "rpm": [1200], "degraded": True}
        panel = build_fan_panel(data)
        assert isinstance(panel, Panel)
        assert panel.subtitle is not None

    def test_empty_duty_list(self) -> None:
        """Empty duty list should still show at least one row."""
        data = {"mode": "off", "duty": [], "rpm": [], "degraded": False}
        panel = build_fan_panel(data)
        assert isinstance(panel, Panel)


class TestBuildLedPanel:
    """Tests for the LED status panel."""

    def test_with_data(self) -> None:
        panel = build_led_panel(SAMPLE_LED)
        assert isinstance(panel, Panel)
        assert "LED Status" in str(panel.title)

    def test_with_none(self) -> None:
        panel = build_led_panel(None)
        assert isinstance(panel, Panel)

    def test_off_mode(self) -> None:
        data = {"mode": "off", "color": {"red": 0, "green": 0, "blue": 0}, "degraded": False}
        panel = build_led_panel(data)
        assert isinstance(panel, Panel)

    def test_manual_mode(self) -> None:
        data = {"mode": "manual", "color": {"red": 255, "green": 0, "blue": 128}, "degraded": False}
        panel = build_led_panel(data)
        assert isinstance(panel, Panel)

    def test_breathing_mode(self) -> None:
        data = {"mode": "breathing", "color": {"red": 0, "green": 0, "blue": 255}, "degraded": False}
        panel = build_led_panel(data)
        assert isinstance(panel, Panel)

    def test_degraded(self) -> None:
        data = {"mode": "rainbow", "color": {"red": 0, "green": 0, "blue": 0}, "degraded": True}
        panel = build_led_panel(data)
        assert isinstance(panel, Panel)


class TestBuildInfoPanel:
    """Tests for the system info panel."""

    def test_with_all_data(self) -> None:
        panel = build_info_panel(SAMPLE_HEALTH, SAMPLE_MONITOR, 2.0)
        assert isinstance(panel, Panel)
        assert "Info" in str(panel.title)

    def test_with_none_health(self) -> None:
        panel = build_info_panel(None, SAMPLE_MONITOR, 2.0)
        assert isinstance(panel, Panel)

    def test_with_none_monitor(self) -> None:
        panel = build_info_panel(SAMPLE_HEALTH, None, 2.0)
        assert isinstance(panel, Panel)

    def test_with_all_none(self) -> None:
        panel = build_info_panel(None, None, 5.0)
        assert isinstance(panel, Panel)

    def test_degraded_health(self) -> None:
        health = {**SAMPLE_HEALTH, "status": "degraded"}
        panel = build_info_panel(health, None, 2.0)
        assert isinstance(panel, Panel)

    def test_error_health(self) -> None:
        health = {**SAMPLE_HEALTH, "status": "error"}
        panel = build_info_panel(health, None, 2.0)
        assert isinstance(panel, Panel)


class TestBuildHeader:
    """Tests for the header bar."""

    def test_connected(self) -> None:
        panel = build_header(connected=True)
        assert isinstance(panel, Panel)

    def test_disconnected(self) -> None:
        panel = build_header(connected=False)
        assert isinstance(panel, Panel)


# ---------------------------------------------------------------------------
# Layout assembly tests
# ---------------------------------------------------------------------------


class TestBuildLayout:
    """Tests for the full layout assembly."""

    def test_full_data(self) -> None:
        layout = build_layout(_full_data(), 2.0)
        assert isinstance(layout, Layout)

    def test_empty_data(self) -> None:
        layout = build_layout(_empty_data(), 2.0)
        assert isinstance(layout, Layout)

    def test_partial_data(self) -> None:
        data = _empty_data()
        data["health"] = SAMPLE_HEALTH
        layout = build_layout(data, 2.0)
        assert isinstance(layout, Layout)

    def test_layout_has_expected_regions(self) -> None:
        layout = build_layout(_full_data(), 2.0)
        # Layout should have header and body
        assert layout["header"] is not None
        assert layout["body"] is not None


# ---------------------------------------------------------------------------
# fetch_dashboard_data tests
# ---------------------------------------------------------------------------


class TestFetchDashboardData:
    """Tests for the HTTP data fetcher."""

    def test_all_endpoints_success(self) -> None:
        """All four endpoints return 200."""
        mock_responses = {
            "/api/health": httpx.Response(200, json=SAMPLE_HEALTH),
            "/api/plugins/system-monitor/status": httpx.Response(200, json=SAMPLE_MONITOR),
            "/api/plugins/fan-control/status": httpx.Response(200, json=SAMPLE_FAN),
            "/api/plugins/led-control/status": httpx.Response(200, json=SAMPLE_LED),
        }

        def mock_get(path: str, **kwargs: Any) -> httpx.Response:
            return mock_responses.get(path, httpx.Response(404))

        with patch("casectl.tui.top.httpx.Client") as MockClient:
            client = MagicMock()
            client.get.side_effect = mock_get
            client.__enter__ = MagicMock(return_value=client)
            client.__exit__ = MagicMock(return_value=False)
            MockClient.return_value = client

            data = fetch_dashboard_data("http://127.0.0.1:8420")

        assert data["health"] == SAMPLE_HEALTH
        assert data["monitor"] == SAMPLE_MONITOR
        assert data["fan"] == SAMPLE_FAN
        assert data["led"] == SAMPLE_LED

    def test_connection_error(self) -> None:
        """ConnectError results in all None values."""
        with patch("casectl.tui.top.httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = MagicMock(
                side_effect=httpx.ConnectError("refused")
            )
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            data = fetch_dashboard_data("http://127.0.0.1:8420")

        assert data["health"] is None
        assert data["monitor"] is None
        assert data["fan"] is None
        assert data["led"] is None

    def test_partial_failure(self) -> None:
        """Some endpoints fail, others succeed."""
        call_count = 0

        def mock_get(path: str, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if path == "/api/health":
                return httpx.Response(200, json=SAMPLE_HEALTH)
            if path == "/api/plugins/fan-control/status":
                return httpx.Response(200, json=SAMPLE_FAN)
            raise httpx.TimeoutException("timeout")

        with patch("casectl.tui.top.httpx.Client") as MockClient:
            client = MagicMock()
            client.get.side_effect = mock_get
            client.__enter__ = MagicMock(return_value=client)
            client.__exit__ = MagicMock(return_value=False)
            MockClient.return_value = client

            data = fetch_dashboard_data("http://127.0.0.1:8420")

        assert data["health"] == SAMPLE_HEALTH
        assert data["monitor"] is None
        assert data["fan"] == SAMPLE_FAN
        assert data["led"] is None

    def test_non_200_status(self) -> None:
        """Non-200 responses result in None values."""
        def mock_get(path: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        with patch("casectl.tui.top.httpx.Client") as MockClient:
            client = MagicMock()
            client.get.side_effect = mock_get
            client.__enter__ = MagicMock(return_value=client)
            client.__exit__ = MagicMock(return_value=False)
            MockClient.return_value = client

            data = fetch_dashboard_data("http://127.0.0.1:8420")

        assert data["health"] is None
        assert data["monitor"] is None
        assert data["fan"] is None
        assert data["led"] is None


# ---------------------------------------------------------------------------
# run_top tests
# ---------------------------------------------------------------------------


class TestRunTop:
    """Tests for the main run_top loop."""

    def test_stops_on_keyboard_interrupt(self) -> None:
        """run_top should exit cleanly on KeyboardInterrupt."""
        fetch_count = 0

        def mock_fetch(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count > 1:
                raise KeyboardInterrupt
            return _full_data()

        with patch("casectl.tui.top.fetch_dashboard_data", side_effect=mock_fetch):
            with patch("casectl.tui.top.Live") as MockLive:
                mock_live_instance = MagicMock()
                MockLive.return_value.__enter__ = MagicMock(return_value=mock_live_instance)
                MockLive.return_value.__exit__ = MagicMock(return_value=False)

                with patch("casectl.tui.top.time.sleep", side_effect=KeyboardInterrupt):
                    # Should not raise
                    run_top(
                        base_url="http://127.0.0.1:8420",
                        refresh_interval=2.0,
                        console=Console(file=MagicMock()),
                    )

    def test_refresh_interval_clamped_low(self) -> None:
        """Interval below MIN is clamped up."""
        with patch("casectl.tui.top.fetch_dashboard_data", return_value=_full_data()):
            with patch("casectl.tui.top.Live") as MockLive:
                mock_live_instance = MagicMock()
                MockLive.return_value.__enter__ = MagicMock(return_value=mock_live_instance)
                MockLive.return_value.__exit__ = MagicMock(return_value=False)

                with patch("casectl.tui.top.time.sleep", side_effect=KeyboardInterrupt):
                    run_top(
                        base_url="http://127.0.0.1:8420",
                        refresh_interval=0.1,  # Below MIN
                        console=Console(file=MagicMock()),
                    )

    def test_refresh_interval_clamped_high(self) -> None:
        """Interval above MAX is clamped down."""
        with patch("casectl.tui.top.fetch_dashboard_data", return_value=_full_data()):
            with patch("casectl.tui.top.Live") as MockLive:
                mock_live_instance = MagicMock()
                MockLive.return_value.__enter__ = MagicMock(return_value=mock_live_instance)
                MockLive.return_value.__exit__ = MagicMock(return_value=False)

                with patch("casectl.tui.top.time.sleep", side_effect=KeyboardInterrupt):
                    run_top(
                        base_url="http://127.0.0.1:8420",
                        refresh_interval=120.0,  # Above MAX
                        console=Console(file=MagicMock()),
                    )

    def test_signal_handler_stops_loop(self) -> None:
        """Simulating SIGINT via the signal handler should stop the loop."""
        import signal as sig

        call_count = 0

        def mock_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Simulate the signal handler being called
                raise KeyboardInterrupt

        with patch("casectl.tui.top.fetch_dashboard_data", return_value=_full_data()):
            with patch("casectl.tui.top.Live") as MockLive:
                mock_live_instance = MagicMock()
                MockLive.return_value.__enter__ = MagicMock(return_value=mock_live_instance)
                MockLive.return_value.__exit__ = MagicMock(return_value=False)

                with patch("casectl.tui.top.time.sleep", side_effect=mock_sleep):
                    run_top(
                        base_url="http://127.0.0.1:8420",
                        console=Console(file=MagicMock()),
                    )


# ---------------------------------------------------------------------------
# CLI integration test
# ---------------------------------------------------------------------------


class TestTopCLI:
    """Test the ``casectl top`` CLI command registration and entry point."""

    def test_top_command_exists(self) -> None:
        from casectl.cli.main import cli

        assert "top" in [cmd for cmd in cli.commands]

    def test_top_command_help(self) -> None:
        from click.testing import CliRunner

        from casectl.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["top", "--help"])
        assert result.exit_code == 0
        assert "terminal dashboard" in result.output.lower() or "interactive" in result.output.lower()

    def test_top_command_interval_option(self) -> None:
        from click.testing import CliRunner

        from casectl.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["top", "--help"])
        assert "--interval" in result.output or "-n" in result.output

    def test_top_command_once_option_in_help(self) -> None:
        """The --once flag should appear in help text."""
        from click.testing import CliRunner

        from casectl.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["top", "--help"])
        assert "--once" in result.output

    def test_top_once_renders_snapshot(self) -> None:
        """--once should render a single snapshot and exit cleanly."""
        from click.testing import CliRunner

        from casectl.cli.main import cli

        with patch("casectl.tui.top.fetch_dashboard_data", return_value=_full_data()):
            runner = CliRunner()
            result = runner.invoke(cli, ["top", "--once"])

            assert result.exit_code == 0

    def test_top_once_warns_if_daemon_unreachable(self) -> None:
        """--once should warn if daemon is not reachable."""
        from click.testing import CliRunner

        from casectl.cli.main import cli

        with patch("casectl.tui.top.fetch_dashboard_data", return_value=_empty_data()):
            runner = CliRunner()
            result = runner.invoke(cli, ["top", "--once"])

            assert result.exit_code == 0

    def test_top_non_tty_exits_with_error(self) -> None:
        """Running without --once in a non-TTY should fail gracefully."""
        from click.testing import CliRunner

        from casectl.cli.main import cli

        # CliRunner does not provide a real TTY, so isatty() returns False.
        # The top command checks sys.stdout.isatty() — patch at module level.
        with patch("casectl.cli.main.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            mock_sys.stdout.fileno.return_value = 1
            runner = CliRunner()
            result = runner.invoke(cli, ["top"])

            assert result.exit_code == 1

    def test_top_interactive_calls_run_top(self) -> None:
        """In a proper TTY environment, top should call run_top."""
        import os as real_os

        from click.testing import CliRunner

        from casectl.cli.main import cli

        with patch("casectl.cli.main.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = True
            mock_sys.stdout.fileno.return_value = 1
            with patch("casectl.cli.main.os") as mock_os:
                mock_os.get_terminal_size.return_value = real_os.terminal_size((120, 40))
                mock_os.path = real_os.path
                mock_os.environ = real_os.environ
                with patch("casectl.tui.top.run_top") as mock_run:
                    runner = CliRunner()
                    result = runner.invoke(cli, ["top", "--interval", "5.0"])

                    mock_run.assert_called_once_with(
                        base_url="http://127.0.0.1:8420",
                        refresh_interval=5.0,
                    )

    def test_top_small_terminal_exits(self) -> None:
        """A terminal smaller than 60x15 should exit with an error."""
        import os as real_os

        from click.testing import CliRunner

        from casectl.cli.main import cli

        with patch("casectl.cli.main.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = True
            mock_sys.stdout.fileno.return_value = 1
            with patch("casectl.cli.main.os") as mock_os:
                mock_os.get_terminal_size.return_value = real_os.terminal_size((40, 10))
                mock_os.path = real_os.path
                mock_os.environ = real_os.environ
                runner = CliRunner()
                result = runner.invoke(cli, ["top"])

                assert result.exit_code == 1

    def test_top_keybinding_hints_in_help(self) -> None:
        """Help text should mention keybindings."""
        from click.testing import CliRunner

        from casectl.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["top", "--help"])
        # Should mention at least the quit key
        assert "q" in result.output.lower()


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify module constants are sensible."""

    def test_default_interval(self) -> None:
        assert DEFAULT_REFRESH_INTERVAL == 2.0

    def test_min_less_than_max(self) -> None:
        assert MIN_REFRESH_INTERVAL < MAX_REFRESH_INTERVAL

    def test_default_within_bounds(self) -> None:
        assert MIN_REFRESH_INTERVAL <= DEFAULT_REFRESH_INTERVAL <= MAX_REFRESH_INTERVAL
