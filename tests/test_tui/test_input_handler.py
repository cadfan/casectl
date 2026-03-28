"""Tests for the ``casectl.tui.input_handler`` keystroke handling module.

Covers key mapping, fan mode cycling, fan speed adjustment, non-blocking
input reading, and the KeyHandler class integration — all without real
terminal or network I/O.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from casectl.tui.input_handler import (
    FAN_MODE_CYCLE,
    SPEED_STEP,
    KeyHandler,
    _is_real_terminal,
    dispatch_fan_mode_cycle,
    dispatch_fan_speed_change,
    read_key_nonblocking,
)


# ---------------------------------------------------------------------------
# Sample dashboard data fixtures
# ---------------------------------------------------------------------------

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


_SENTINEL = object()


def _dashboard_data(
    fan: dict[str, Any] | None | object = _SENTINEL,
    led: dict[str, Any] | None | object = _SENTINEL,
) -> dict[str, Any]:
    return {
        "health": {"status": "healthy"},
        "monitor": None,
        "fan": SAMPLE_FAN if fan is _SENTINEL else fan,
        "led": SAMPLE_LED if led is _SENTINEL else led,
    }


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify module constants are sensible."""

    def test_fan_mode_cycle_has_modes(self) -> None:
        assert len(FAN_MODE_CYCLE) >= 3

    def test_fan_mode_cycle_types(self) -> None:
        for mode in FAN_MODE_CYCLE:
            assert isinstance(mode, str)

    def test_speed_step_positive(self) -> None:
        assert SPEED_STEP > 0

    def test_speed_step_reasonable(self) -> None:
        assert SPEED_STEP <= 25  # Not too aggressive


# ---------------------------------------------------------------------------
# _is_real_terminal tests
# ---------------------------------------------------------------------------


class TestIsRealTerminal:
    """Tests for terminal detection."""

    def test_returns_false_when_no_fileno(self) -> None:
        with patch("casectl.tui.input_handler.sys") as mock_sys:
            mock_sys.stdin = MagicMock(spec=[])  # No fileno attribute
            # _is_real_terminal catches AttributeError
            result = _is_real_terminal()
            assert result is False

    def test_returns_false_when_not_tty(self) -> None:
        with patch("casectl.tui.input_handler.os.isatty", return_value=False):
            with patch("casectl.tui.input_handler.sys") as mock_sys:
                mock_sys.stdin.fileno.return_value = 0
                result = _is_real_terminal()
                assert result is False

    def test_returns_true_when_tty(self) -> None:
        with patch("casectl.tui.input_handler.os.isatty", return_value=True):
            with patch("casectl.tui.input_handler.sys") as mock_sys:
                mock_sys.stdin.fileno.return_value = 0
                result = _is_real_terminal()
                assert result is True

    def test_returns_false_on_oserror(self) -> None:
        with patch("casectl.tui.input_handler.sys") as mock_sys:
            mock_sys.stdin.fileno.side_effect = OSError("bad fd")
            result = _is_real_terminal()
            assert result is False

    def test_returns_false_on_value_error(self) -> None:
        with patch("casectl.tui.input_handler.sys") as mock_sys:
            mock_sys.stdin.fileno.side_effect = ValueError("closed")
            result = _is_real_terminal()
            assert result is False


# ---------------------------------------------------------------------------
# read_key_nonblocking tests
# ---------------------------------------------------------------------------


class TestReadKeyNonblocking:
    """Tests for the non-blocking key reader."""

    def test_returns_none_when_not_terminal(self) -> None:
        with patch("casectl.tui.input_handler._is_real_terminal", return_value=False):
            assert read_key_nonblocking() is None

    def test_returns_none_on_import_error(self) -> None:
        """When termios is not available (e.g. Windows)."""
        with patch("casectl.tui.input_handler._is_real_terminal", return_value=True):
            with patch("builtins.__import__", side_effect=ImportError("no termios")):
                assert read_key_nonblocking() is None

    def test_returns_char_when_input_available(self) -> None:
        """Simulate a key being available via select."""
        with patch("casectl.tui.input_handler._is_real_terminal", return_value=True):
            mock_fd = 0
            mock_old_settings = [0] * 7

            with (
                patch("casectl.tui.input_handler.sys") as mock_sys,
                patch("casectl.tui.input_handler.os") as mock_os,
            ):
                mock_sys.stdin.fileno.return_value = mock_fd
                mock_os.isatty.return_value = True
                mock_os.read.return_value = b"m"

                # Mock the termios/tty/select imports inside the function
                import types

                mock_termios = types.ModuleType("termios")
                mock_termios.tcgetattr = MagicMock(return_value=mock_old_settings)
                mock_termios.tcsetattr = MagicMock()
                mock_termios.TCSADRAIN = 1
                mock_termios.error = OSError

                mock_tty = types.ModuleType("tty")
                mock_tty.setraw = MagicMock()

                mock_select = types.ModuleType("select")
                mock_select.select = MagicMock(return_value=([mock_fd], [], []))

                with patch.dict("sys.modules", {
                    "termios": mock_termios,
                    "tty": mock_tty,
                    "select": mock_select,
                }):
                    result = read_key_nonblocking()

                assert result == "m"

    def test_returns_none_when_no_input(self) -> None:
        """No key pressed — select returns empty."""
        with patch("casectl.tui.input_handler._is_real_terminal", return_value=True):
            mock_fd = 0
            mock_old_settings = [0] * 7

            with (
                patch("casectl.tui.input_handler.sys") as mock_sys,
                patch("casectl.tui.input_handler.os"),
            ):
                mock_sys.stdin.fileno.return_value = mock_fd

                import types

                mock_termios = types.ModuleType("termios")
                mock_termios.tcgetattr = MagicMock(return_value=mock_old_settings)
                mock_termios.tcsetattr = MagicMock()
                mock_termios.TCSADRAIN = 1
                mock_termios.error = OSError

                mock_tty = types.ModuleType("tty")
                mock_tty.setraw = MagicMock()

                mock_select = types.ModuleType("select")
                mock_select.select = MagicMock(return_value=([], [], []))

                with patch.dict("sys.modules", {
                    "termios": mock_termios,
                    "tty": mock_tty,
                    "select": mock_select,
                }):
                    result = read_key_nonblocking()

                assert result is None


# ---------------------------------------------------------------------------
# dispatch_fan_mode_cycle tests
# ---------------------------------------------------------------------------


class TestDispatchFanModeCycle:
    """Tests for the fan mode cycling dispatcher."""

    def _mock_client(self, response_json: dict, status_code: int = 200) -> MagicMock:
        client = MagicMock()
        resp = httpx.Response(status_code, json=response_json)
        client.put.return_value = resp
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        return client

    def test_cycle_from_follow_temp(self) -> None:
        """follow-temp -> follow-rpi."""
        client = self._mock_client({"status": "ok", "mode": "follow-rpi"})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", "follow_temp")

        assert result is not None
        assert result["mode"] == "follow-rpi"
        client.put.assert_called_once_with(
            "/api/plugins/fan-control/mode",
            json={"mode": "follow-rpi"},
        )

    def test_cycle_from_follow_rpi(self) -> None:
        """follow-rpi -> manual."""
        client = self._mock_client({"status": "ok", "mode": "manual"})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", "follow_rpi")

        assert result is not None
        client.put.assert_called_once_with(
            "/api/plugins/fan-control/mode",
            json={"mode": "manual"},
        )

    def test_cycle_from_manual(self) -> None:
        """manual -> off."""
        client = self._mock_client({"status": "ok", "mode": "off"})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", "manual")

        assert result is not None
        client.put.assert_called_once_with(
            "/api/plugins/fan-control/mode",
            json={"mode": "off"},
        )

    def test_cycle_wraps_around(self) -> None:
        """off -> follow-temp (wraps around)."""
        client = self._mock_client({"status": "ok", "mode": "follow-temp"})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", "off")

        assert result is not None
        client.put.assert_called_once_with(
            "/api/plugins/fan-control/mode",
            json={"mode": "follow-temp"},
        )

    def test_cycle_from_none_starts_at_first(self) -> None:
        """None mode -> follow-temp."""
        client = self._mock_client({"status": "ok", "mode": "follow-temp"})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", None)

        assert result is not None
        client.put.assert_called_once_with(
            "/api/plugins/fan-control/mode",
            json={"mode": "follow-temp"},
        )

    def test_cycle_from_unknown_starts_at_first(self) -> None:
        """Unknown mode -> follow-temp."""
        client = self._mock_client({"status": "ok", "mode": "follow-temp"})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", "custom")

        assert result is not None
        client.put.assert_called_once_with(
            "/api/plugins/fan-control/mode",
            json={"mode": "follow-temp"},
        )

    def test_normalises_underscores(self) -> None:
        """API returns 'follow_temp' with underscores — should still match."""
        client = self._mock_client({"status": "ok", "mode": "follow-rpi"})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", "follow_temp")

        assert result is not None

    def test_returns_none_on_http_error(self) -> None:
        """Non-200 response returns None."""
        client = self._mock_client({"error": "bad"}, status_code=500)
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_mode_cycle("http://localhost:8420", "manual")

        assert result is None

    def test_returns_none_on_connection_error(self) -> None:
        """Connection error returns None."""
        with patch("casectl.tui.input_handler.httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = MagicMock(
                side_effect=httpx.ConnectError("refused")
            )
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            result = dispatch_fan_mode_cycle("http://localhost:8420", "manual")

        assert result is None


# ---------------------------------------------------------------------------
# dispatch_fan_speed_change tests
# ---------------------------------------------------------------------------


class TestDispatchFanSpeedChange:
    """Tests for the fan speed adjustment dispatcher."""

    def _mock_client(self, response_json: dict, status_code: int = 200) -> MagicMock:
        client = MagicMock()
        resp = httpx.Response(status_code, json=response_json)
        client.put.return_value = resp
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        return client

    def test_increase_from_current_duty(self) -> None:
        """Increase speed from current duty [128, 128, 128] (~50%) by 10%."""
        client = self._mock_client({"status": "ok", "duty_hw": [153, 153, 153]})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[128, 128, 128],
                delta=10,
            )

        assert result is not None
        call_args = client.put.call_args
        assert call_args[0][0] == "/api/plugins/fan-control/speed"
        duty = call_args[1]["json"]["duty"]
        assert all(d == 60 for d in duty)  # 50% + 10% = 60%

    def test_decrease_from_current_duty(self) -> None:
        """Decrease speed from current duty [128, 128, 128] (~50%) by 10%."""
        client = self._mock_client({"status": "ok", "duty_hw": [102, 102, 102]})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[128, 128, 128],
                delta=-10,
            )

        assert result is not None
        call_args = client.put.call_args
        duty = call_args[1]["json"]["duty"]
        assert all(d == 40 for d in duty)  # 50% - 10% = 40%

    def test_clamp_at_100(self) -> None:
        """Speed should not exceed 100%."""
        client = self._mock_client({"status": "ok", "duty_hw": [255, 255, 255]})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[250, 250, 250],  # ~98%
                delta=10,
            )

        assert result is not None
        call_args = client.put.call_args
        duty = call_args[1]["json"]["duty"]
        assert all(d == 100 for d in duty)

    def test_clamp_at_0(self) -> None:
        """Speed should not go below 0%."""
        client = self._mock_client({"status": "ok", "duty_hw": [0, 0, 0]})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[10, 10, 10],  # ~4%
                delta=-10,
            )

        assert result is not None
        call_args = client.put.call_args
        duty = call_args[1]["json"]["duty"]
        assert all(d == 0 for d in duty)

    def test_none_duty_defaults_to_50(self) -> None:
        """When current_duty is None, assume 50% baseline."""
        client = self._mock_client({"status": "ok", "duty_hw": [153, 153, 153]})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=None,
                delta=10,
            )

        assert result is not None
        call_args = client.put.call_args
        duty = call_args[1]["json"]["duty"]
        assert all(d == 60 for d in duty)  # 50% + 10% = 60%

    def test_empty_duty_defaults_to_50(self) -> None:
        """When current_duty is empty list, assume 50% baseline."""
        client = self._mock_client({"status": "ok", "duty_hw": [102, 102, 102]})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[],
                delta=-10,
            )

        assert result is not None
        call_args = client.put.call_args
        duty = call_args[1]["json"]["duty"]
        assert all(d == 40 for d in duty)  # 50% - 10% = 40%

    def test_mixed_duty_averages(self) -> None:
        """Mixed duty values should be averaged then adjusted."""
        # [128, 100, 75] avg = (128+100+75)/3 * 100/255 ~ 39%
        client = self._mock_client({"status": "ok", "duty_hw": [127, 127, 127]})
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[128, 100, 75],
                delta=10,
            )

        assert result is not None
        call_args = client.put.call_args
        duty = call_args[1]["json"]["duty"]
        # avg_pct = int((128+100+75)/3 * 100/255) = int(39.6) = 39
        # new = 39 + 10 = 49
        assert all(d == 49 for d in duty)

    def test_returns_none_on_http_error(self) -> None:
        client = self._mock_client({"error": "bad"}, status_code=400)
        with patch("casectl.tui.input_handler.httpx.Client", return_value=client):
            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[128, 128, 128],
                delta=10,
            )

        assert result is None

    def test_returns_none_on_connection_error(self) -> None:
        with patch("casectl.tui.input_handler.httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = MagicMock(
                side_effect=httpx.ConnectError("refused")
            )
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            result = dispatch_fan_speed_change(
                "http://localhost:8420",
                current_duty=[128, 128, 128],
                delta=10,
            )

        assert result is None


# ---------------------------------------------------------------------------
# KeyHandler tests
# ---------------------------------------------------------------------------


class TestKeyHandler:
    """Tests for the KeyHandler class."""

    def test_quit_on_q(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        assert handler.quit_requested is False
        handler.handle_key("q", _dashboard_data())
        assert handler.quit_requested is True

    def test_quit_does_not_dispatch(self) -> None:
        """Pressing 'q' should not make any API calls."""
        handler = KeyHandler("http://localhost:8420")
        with patch("casectl.tui.input_handler.dispatch_fan_mode_cycle") as mock_mode:
            with patch("casectl.tui.input_handler.dispatch_fan_speed_change") as mock_speed:
                handler.handle_key("q", _dashboard_data())

        mock_mode.assert_not_called()
        mock_speed.assert_not_called()

    def test_m_cycles_fan_mode(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        with patch(
            "casectl.tui.input_handler.dispatch_fan_mode_cycle",
            return_value={"status": "ok", "mode": "follow-rpi"},
        ) as mock_cycle:
            handler.handle_key("m", _dashboard_data())

        mock_cycle.assert_called_once_with(
            "http://localhost:8420", "follow_temp"
        )
        assert handler.last_action == "Fan mode -> follow-rpi"
        assert handler.quit_requested is False

    def test_m_with_no_fan_data(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        data = _dashboard_data(fan=None)
        with patch(
            "casectl.tui.input_handler.dispatch_fan_mode_cycle",
            return_value={"status": "ok", "mode": "follow-temp"},
        ) as mock_cycle:
            handler.handle_key("m", data)

        mock_cycle.assert_called_once_with(
            "http://localhost:8420", None
        )

    def test_m_failure_sets_error_action(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        with patch(
            "casectl.tui.input_handler.dispatch_fan_mode_cycle",
            return_value=None,
        ):
            handler.handle_key("m", _dashboard_data())

        assert handler.last_action == "Fan mode change failed"

    def test_plus_increases_speed(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        with patch(
            "casectl.tui.input_handler.dispatch_fan_speed_change",
            return_value={"status": "ok", "duty_hw": [153, 153, 153]},
        ) as mock_speed:
            handler.handle_key("+", _dashboard_data())

        mock_speed.assert_called_once_with(
            "http://localhost:8420",
            [128, 100, 75],
            delta=SPEED_STEP,
        )
        assert handler.last_action == f"Fan speed +{SPEED_STEP}%"

    def test_equals_increases_speed(self) -> None:
        """'=' should also increase speed (same key on US keyboards without shift)."""
        handler = KeyHandler("http://localhost:8420")
        with patch(
            "casectl.tui.input_handler.dispatch_fan_speed_change",
            return_value={"status": "ok", "duty_hw": [153, 153, 153]},
        ) as mock_speed:
            handler.handle_key("=", _dashboard_data())

        mock_speed.assert_called_once()
        assert "+" in (handler.last_action or "")

    def test_minus_decreases_speed(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        with patch(
            "casectl.tui.input_handler.dispatch_fan_speed_change",
            return_value={"status": "ok", "duty_hw": [102, 102, 102]},
        ) as mock_speed:
            handler.handle_key("-", _dashboard_data())

        mock_speed.assert_called_once_with(
            "http://localhost:8420",
            [128, 100, 75],
            delta=-SPEED_STEP,
        )
        assert handler.last_action == f"Fan speed -{SPEED_STEP}%"

    def test_speed_with_no_fan_data(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        data = _dashboard_data(fan=None)
        with patch(
            "casectl.tui.input_handler.dispatch_fan_speed_change",
            return_value={"status": "ok", "duty_hw": [153, 153, 153]},
        ) as mock_speed:
            handler.handle_key("+", data)

        mock_speed.assert_called_once_with(
            "http://localhost:8420",
            None,
            delta=SPEED_STEP,
        )

    def test_speed_failure_sets_error_action(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        with patch(
            "casectl.tui.input_handler.dispatch_fan_speed_change",
            return_value=None,
        ):
            handler.handle_key("+", _dashboard_data())

        assert handler.last_action == "Fan speed change failed"

    def test_unknown_key_does_nothing(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        with patch("casectl.tui.input_handler.dispatch_fan_mode_cycle") as mock_mode:
            with patch("casectl.tui.input_handler.dispatch_fan_speed_change") as mock_speed:
                handler.handle_key("x", _dashboard_data())

        mock_mode.assert_not_called()
        mock_speed.assert_not_called()
        assert handler.quit_requested is False
        assert handler.last_action is None

    def test_initial_state(self) -> None:
        handler = KeyHandler("http://localhost:8420")
        assert handler.quit_requested is False
        assert handler.last_action is None
        assert handler.base_url == "http://localhost:8420"

    def test_multiple_keys_in_sequence(self) -> None:
        """Multiple keypresses update state correctly."""
        handler = KeyHandler("http://localhost:8420")

        with patch(
            "casectl.tui.input_handler.dispatch_fan_mode_cycle",
            return_value={"status": "ok", "mode": "manual"},
        ):
            handler.handle_key("m", _dashboard_data())
        assert handler.last_action == "Fan mode -> manual"

        with patch(
            "casectl.tui.input_handler.dispatch_fan_speed_change",
            return_value={"status": "ok", "duty_hw": [153, 153, 153]},
        ):
            handler.handle_key("+", _dashboard_data())
        assert "speed" in (handler.last_action or "").lower()

        handler.handle_key("q", _dashboard_data())
        assert handler.quit_requested is True


# ---------------------------------------------------------------------------
# Integration: updated build_header tests
# ---------------------------------------------------------------------------


class TestBuildHeaderWithAction:
    """Tests for build_header with last_action parameter."""

    def test_header_with_no_action(self) -> None:
        from casectl.tui.top import build_header

        panel = build_header(connected=True, last_action=None)
        assert panel is not None

    def test_header_with_action(self) -> None:
        from casectl.tui.top import build_header

        panel = build_header(connected=True, last_action="Fan mode -> manual")
        assert panel is not None

    def test_header_shows_keybinding_hints(self) -> None:
        from rich.console import Console

        from casectl.tui.top import build_header

        panel = build_header(connected=True)
        # Render to string to check content
        console = Console(file=MagicMock(), width=120)
        with console.capture() as capture:
            console.print(panel)
        output = capture.get()
        assert "m" in output
        assert "speed" in output
        assert "quit" in output

    def test_header_disconnected_with_action(self) -> None:
        from casectl.tui.top import build_header

        panel = build_header(connected=False, last_action="Fan speed +10%")
        assert panel is not None


# ---------------------------------------------------------------------------
# Integration: build_layout with last_action
# ---------------------------------------------------------------------------


class TestBuildLayoutWithAction:
    """Tests for build_layout with last_action parameter."""

    def test_layout_accepts_last_action(self) -> None:
        from casectl.tui.top import build_layout

        data = {
            "health": {"status": "healthy", "version": "0.1.0", "uptime": 100, "plugins": []},
            "monitor": None,
            "fan": SAMPLE_FAN,
            "led": SAMPLE_LED,
        }
        layout = build_layout(data, 2.0, last_action="Fan mode -> off")
        assert layout is not None

    def test_layout_without_last_action(self) -> None:
        from casectl.tui.top import build_layout

        data = {"health": None, "monitor": None, "fan": None, "led": None}
        layout = build_layout(data, 2.0)
        assert layout is not None
