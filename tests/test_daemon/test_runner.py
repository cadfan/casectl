"""Tests for casectl.daemon.runner — daemon wiring helpers."""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.daemon.runner import (
    _configure_logging,
    _init_expansion_board,
    _init_oled,
    _init_system_info,
    _shutdown,
)


# ---------------------------------------------------------------------------
# _init_expansion_board
# ---------------------------------------------------------------------------


class TestInitExpansionBoard:
    """Verify expansion board initialisation helper."""

    def test_init_expansion_board_success(self) -> None:
        """When hardware is present and imports succeed, return an ExpansionBoard."""
        mock_expansion = MagicMock()
        mock_expansion.connected = True

        mock_fan_hw_mode = MagicMock()

        with (
            patch(
                "casectl.daemon.runner.is_case_hardware_present",
                return_value=True,
                create=True,
            ),
            patch(
                "casectl.hardware.detect.is_case_hardware_present",
                return_value=True,
            ),
            patch.dict("sys.modules", {}),
        ):
            # Patch imports inside _init_expansion_board
            with (
                patch(
                    "casectl.daemon.runner.is_case_hardware_present",
                    return_value=True,
                    create=True,
                ),
            ):
                # We need to mock the imports that happen inside the function
                mock_detect_module = MagicMock()
                mock_detect_module.is_case_hardware_present.return_value = True

                mock_expansion_module = MagicMock()
                mock_expansion_module.ExpansionBoard.return_value = mock_expansion
                mock_expansion_module.FanHwMode.MANUAL = "MANUAL"

                with patch.dict(
                    "sys.modules",
                    {
                        "casectl.hardware.detect": mock_detect_module,
                        "casectl.hardware.expansion": mock_expansion_module,
                    },
                ):
                    result = _init_expansion_board()

        assert result is mock_expansion

    def test_init_expansion_board_not_present(self) -> None:
        """When hardware detection says not present, return None."""
        mock_detect_module = MagicMock()
        mock_detect_module.is_case_hardware_present.return_value = False

        with patch.dict(
            "sys.modules",
            {"casectl.hardware.detect": mock_detect_module},
        ):
            result = _init_expansion_board()

        assert result is None

    def test_init_expansion_board_import_error(self) -> None:
        """When casectl.hardware.detect cannot be imported, return None."""
        with patch(
            "builtins.__import__",
            side_effect=_make_import_blocker("casectl.hardware.detect"),
        ):
            result = _init_expansion_board()

        assert result is None


# ---------------------------------------------------------------------------
# _init_oled
# ---------------------------------------------------------------------------


class TestInitOled:
    """Verify OLED initialisation helper."""

    def test_init_oled_success(self) -> None:
        """When OLED is detected and initialised, return the OledDevice."""
        mock_oled = MagicMock()
        mock_oled.available = True

        mock_detect_module = MagicMock()
        mock_detect_module.is_oled_present.return_value = True

        mock_oled_module = MagicMock()
        mock_oled_module.OledDevice.return_value = mock_oled

        with patch.dict(
            "sys.modules",
            {
                "casectl.hardware.detect": mock_detect_module,
                "casectl.hardware.oled": mock_oled_module,
            },
        ):
            result = _init_oled(rotation_degrees=0)

        assert result is mock_oled
        mock_oled_module.OledDevice.assert_called_once_with(rotation=0)

    def test_init_oled_not_present(self) -> None:
        """When OLED is not detected on I2C, return None."""
        mock_detect_module = MagicMock()
        mock_detect_module.is_oled_present.return_value = False

        with patch.dict(
            "sys.modules",
            {"casectl.hardware.detect": mock_detect_module},
        ):
            result = _init_oled(rotation_degrees=0)

        assert result is None

    def test_init_oled_rotation_mapping(self) -> None:
        """Verify 180 degrees maps to rotation index 2."""
        mock_oled = MagicMock()
        mock_oled.available = True

        mock_detect_module = MagicMock()
        mock_detect_module.is_oled_present.return_value = True

        mock_oled_module = MagicMock()
        mock_oled_module.OledDevice.return_value = mock_oled

        with patch.dict(
            "sys.modules",
            {
                "casectl.hardware.detect": mock_detect_module,
                "casectl.hardware.oled": mock_oled_module,
            },
        ):
            result = _init_oled(rotation_degrees=180)

        mock_oled_module.OledDevice.assert_called_once_with(rotation=2)


# ---------------------------------------------------------------------------
# _init_system_info
# ---------------------------------------------------------------------------


class TestInitSystemInfo:
    """Verify SystemInfo initialisation helper."""

    def test_init_system_info_success(self) -> None:
        """When casectl.hardware.system imports fine, return a SystemInfo."""
        mock_system_info = MagicMock()
        mock_system_module = MagicMock()
        mock_system_module.SystemInfo.return_value = mock_system_info

        with patch.dict(
            "sys.modules",
            {"casectl.hardware.system": mock_system_module},
        ):
            result = _init_system_info()

        assert result is mock_system_info

    def test_init_system_info_import_error(self) -> None:
        """When psutil is missing (import fails), return None."""
        with patch(
            "builtins.__import__",
            side_effect=_make_import_blocker("casectl.hardware.system"),
        ):
            result = _init_system_info()

        assert result is None


# ---------------------------------------------------------------------------
# _configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    """Verify logging configuration helper."""

    def test_configure_logging(self) -> None:
        """_configure_logging should add a handler and set the level."""
        root = logging.getLogger()
        # Clear existing handlers to test fresh setup
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            _configure_logging(level=logging.DEBUG)
            assert len(root.handlers) >= 1
            assert root.level == logging.DEBUG
        finally:
            # Restore original handlers
            root.handlers = original_handlers

    def test_configure_logging_no_duplicate_handlers(self) -> None:
        """Calling _configure_logging twice should not add duplicate handlers."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            _configure_logging(level=logging.INFO)
            count_after_first = len(root.handlers)
            _configure_logging(level=logging.WARNING)
            count_after_second = len(root.handlers)
            # Should not have added more handlers the second time
            assert count_after_second == count_after_first
        finally:
            root.handlers = original_handlers


# ---------------------------------------------------------------------------
# _shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """Verify shutdown cleanup sequence."""

    def test_shutdown_cleanup(self) -> None:
        """_shutdown should stop plugins, close hardware, and tell uvicorn to exit."""
        mock_server = MagicMock()
        mock_server.should_exit = False

        mock_plugin_host = MagicMock()
        mock_plugin_host.stop_all = AsyncMock()

        mock_expansion = MagicMock()
        mock_oled = MagicMock()

        asyncio.run(
            _shutdown(mock_server, mock_plugin_host, mock_expansion, mock_oled)
        )

        mock_plugin_host.stop_all.assert_awaited_once()
        mock_expansion.close.assert_called_once()
        mock_oled.close.assert_called_once()
        assert mock_server.should_exit is True

    def test_shutdown_with_none_hardware(self) -> None:
        """_shutdown handles None expansion and oled gracefully."""
        mock_server = MagicMock()
        mock_server.should_exit = False

        mock_plugin_host = MagicMock()
        mock_plugin_host.stop_all = AsyncMock()

        asyncio.run(
            _shutdown(mock_server, mock_plugin_host, None, None)
        )

        mock_plugin_host.stop_all.assert_awaited_once()
        assert mock_server.should_exit is True

    def test_shutdown_plugin_error_continues(self) -> None:
        """If plugin_host.stop_all raises, cleanup still continues."""
        mock_server = MagicMock()
        mock_server.should_exit = False

        mock_plugin_host = MagicMock()
        mock_plugin_host.stop_all = AsyncMock(side_effect=RuntimeError("plugin error"))

        mock_expansion = MagicMock()
        mock_oled = MagicMock()

        # Should not raise
        asyncio.run(
            _shutdown(mock_server, mock_plugin_host, mock_expansion, mock_oled)
        )

        # Hardware cleanup still happened despite plugin error
        mock_expansion.close.assert_called_once()
        mock_oled.close.assert_called_once()
        assert mock_server.should_exit is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_import_blocker(blocked_module: str):
    """Return a side_effect function that blocks one module import.

    All other imports pass through to the real __import__.
    """
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _blocking_import(name, *args, **kwargs):
        if name == blocked_module:
            raise ImportError(f"Mocked ImportError for {name}")
        return real_import(name, *args, **kwargs)

    return _blocking_import
