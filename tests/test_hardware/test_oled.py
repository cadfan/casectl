"""Tests for casectl.hardware.oled — SSD1306 OLED display wrapper."""

from __future__ import annotations

import asyncio
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to import OledDevice with/without luma mocked
# ---------------------------------------------------------------------------


def _import_oled_with_luma(
    mock_i2c: MagicMock, mock_ssd1306: MagicMock
) -> type:
    """Import casectl.hardware.oled with luma mocked as available."""
    mock_luma_serial = MagicMock()
    mock_luma_serial.i2c = mock_i2c

    mock_luma_device = MagicMock()
    mock_luma_device.ssd1306 = mock_ssd1306

    with patch.dict(
        "sys.modules",
        {
            "luma": MagicMock(),
            "luma.core": MagicMock(),
            "luma.core.interface": MagicMock(),
            "luma.core.interface.serial": mock_luma_serial,
            "luma.oled": MagicMock(),
            "luma.oled.device": mock_luma_device,
        },
    ):
        import casectl.hardware.oled as oled_mod

        importlib.reload(oled_mod)
        return oled_mod


def _import_oled_without_luma() -> type:
    """Import casectl.hardware.oled with luma unavailable."""
    # Temporarily remove luma modules and make them raise ImportError
    saved = {}
    luma_keys = [k for k in sys.modules if k.startswith("luma")]
    for k in luma_keys:
        saved[k] = sys.modules.pop(k)

    with patch.dict(
        "sys.modules",
        {
            "luma": None,
            "luma.core": None,
            "luma.core.interface": None,
            "luma.core.interface.serial": None,
            "luma.oled": None,
            "luma.oled.device": None,
        },
    ):
        import casectl.hardware.oled as oled_mod

        importlib.reload(oled_mod)
        return oled_mod


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestOledInit:
    """Verify OledDevice initialisation with and without luma."""

    def test_oled_init_without_luma(self) -> None:
        """When luma is not installed, available should be False."""
        oled_mod = _import_oled_without_luma()
        try:
            device = oled_mod.OledDevice()
            assert device.available is False
        finally:
            importlib.reload(oled_mod)

    def test_oled_init_with_luma(self) -> None:
        """When luma is installed and device creation succeeds, available is True."""
        mock_i2c = MagicMock()
        mock_serial_instance = MagicMock()
        mock_i2c.return_value = mock_serial_instance

        mock_ssd1306 = MagicMock()
        mock_device_instance = MagicMock()
        mock_ssd1306.return_value = mock_device_instance

        oled_mod = _import_oled_with_luma(mock_i2c, mock_ssd1306)
        try:
            device = oled_mod.OledDevice()
            assert device.available is True
            mock_i2c.assert_called_once_with(port=1, address=0x3C)
            mock_ssd1306.assert_called_once_with(mock_serial_instance, rotate=0)
        finally:
            importlib.reload(oled_mod)


# ---------------------------------------------------------------------------
# Display operations
# ---------------------------------------------------------------------------


class TestRenderImage:
    """Verify render_image delegates to the underlying luma device."""

    def test_render_image(self) -> None:
        """render_image should call device.display with the provided image."""
        mock_i2c = MagicMock()
        mock_ssd1306 = MagicMock()
        mock_device_instance = MagicMock()
        mock_ssd1306.return_value = mock_device_instance

        oled_mod = _import_oled_with_luma(mock_i2c, mock_ssd1306)
        try:
            oled = oled_mod.OledDevice()
            assert oled.available is True

            fake_image = MagicMock()
            oled.render_image(fake_image)
            mock_device_instance.display.assert_called_once_with(fake_image)
        finally:
            importlib.reload(oled_mod)

    def test_render_image_failure_sets_unavailable(self) -> None:
        """When device.display raises, available becomes False."""
        mock_i2c = MagicMock()
        mock_ssd1306 = MagicMock()
        mock_device_instance = MagicMock()
        mock_device_instance.display.side_effect = RuntimeError("I2C failure")
        mock_ssd1306.return_value = mock_device_instance

        oled_mod = _import_oled_with_luma(mock_i2c, mock_ssd1306)
        try:
            oled = oled_mod.OledDevice()
            assert oled.available is True

            fake_image = MagicMock()
            oled.render_image(fake_image)
            assert oled.available is False
        finally:
            importlib.reload(oled_mod)


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


class TestClear:
    """Verify clear blanks the display."""

    def test_clear(self) -> None:
        """clear() should call device.hide() then device.show()."""
        mock_i2c = MagicMock()
        mock_ssd1306 = MagicMock()
        mock_device_instance = MagicMock()
        mock_ssd1306.return_value = mock_device_instance

        oled_mod = _import_oled_with_luma(mock_i2c, mock_ssd1306)
        try:
            oled = oled_mod.OledDevice()
            oled.clear()
            mock_device_instance.hide.assert_called_once()
            mock_device_instance.show.assert_called_once()
        finally:
            importlib.reload(oled_mod)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    """Verify context manager calls close on exit."""

    def test_context_manager(self) -> None:
        """Exiting the context manager should call cleanup on the device."""
        mock_i2c = MagicMock()
        mock_ssd1306 = MagicMock()
        mock_device_instance = MagicMock()
        mock_ssd1306.return_value = mock_device_instance

        oled_mod = _import_oled_with_luma(mock_i2c, mock_ssd1306)
        try:
            with oled_mod.OledDevice() as oled:
                assert oled.available is True

            # After exit, close() was called which calls cleanup
            mock_device_instance.cleanup.assert_called_once()
            assert oled.available is False
        finally:
            importlib.reload(oled_mod)


# ---------------------------------------------------------------------------
# Async render
# ---------------------------------------------------------------------------


class TestAsyncRenderImage:
    """Verify async_render_image delegates to render_image via asyncio.to_thread."""

    def test_async_render_image(self) -> None:
        """async_render_image should call render_image in a thread."""
        mock_i2c = MagicMock()
        mock_ssd1306 = MagicMock()
        mock_device_instance = MagicMock()
        mock_ssd1306.return_value = mock_device_instance

        oled_mod = _import_oled_with_luma(mock_i2c, mock_ssd1306)
        try:
            oled = oled_mod.OledDevice()
            fake_image = MagicMock()

            asyncio.run(oled.async_render_image(fake_image))
            mock_device_instance.display.assert_called_once_with(fake_image)
        finally:
            importlib.reload(oled_mod)
