"""Tests for casectl.hardware.detect — hardware detection utilities."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from casectl.hardware.detect import (
    DEVICE_MODEL_PATH,
    DEVICE_REVISION_PATH,
    DEVICE_SERIAL_PATH,
    I2C_DEVICE_PATH,
    check_i2c_permissions,
    get_platform_info,
    is_case_hardware_present,
    is_oled_present,
    is_raspberry_pi,
)


# ---------------------------------------------------------------------------
# is_raspberry_pi
# ---------------------------------------------------------------------------


class TestIsRaspberryPi:
    """Verify Raspberry Pi detection via sysfs model file."""

    def test_is_raspberry_pi_true(self) -> None:
        """When model file contains 'Raspberry Pi 5', return True."""
        with patch.object(
            Path, "read_text", return_value="Raspberry Pi 5 Model B Rev 1.0\x00"
        ):
            assert is_raspberry_pi() is True

    def test_is_raspberry_pi_false_on_laptop(self) -> None:
        """When model file does not exist, return False."""
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert is_raspberry_pi() is False


# ---------------------------------------------------------------------------
# is_case_hardware_present
# ---------------------------------------------------------------------------


class TestIsCaseHardwarePresent:
    """Verify STM32 expansion board detection via I2C probe."""

    def test_is_case_hardware_present_true(self) -> None:
        """When smbus2 read at 0x21 succeeds, return True."""
        mock_smb = MagicMock()
        mock_smb.__enter__ = MagicMock(return_value=mock_smb)
        mock_smb.__exit__ = MagicMock(return_value=False)
        mock_smb.read_byte = MagicMock(return_value=0)

        mock_smbus2 = MagicMock()
        mock_smbus2.SMBus.return_value = mock_smb

        with patch.dict("sys.modules", {"smbus2": mock_smbus2}):
            # Re-import to pick up the mocked module
            import importlib
            import casectl.hardware.detect as detect_mod

            importlib.reload(detect_mod)
            try:
                assert detect_mod.is_case_hardware_present() is True
            finally:
                importlib.reload(detect_mod)

    def test_is_case_hardware_present_no_smbus(self) -> None:
        """When smbus2 is not installed, return False."""
        with patch.dict("sys.modules", {"smbus2": None}):
            import importlib
            import casectl.hardware.detect as detect_mod

            importlib.reload(detect_mod)
            try:
                assert detect_mod.is_case_hardware_present() is False
            finally:
                importlib.reload(detect_mod)


# ---------------------------------------------------------------------------
# is_oled_present
# ---------------------------------------------------------------------------


class TestIsOledPresent:
    """Verify SSD1306 OLED display detection via I2C probe."""

    def test_is_oled_present_true(self) -> None:
        """When smbus2 read at 0x3C succeeds, return True."""
        mock_smb = MagicMock()
        mock_smb.__enter__ = MagicMock(return_value=mock_smb)
        mock_smb.__exit__ = MagicMock(return_value=False)
        mock_smb.read_byte = MagicMock(return_value=0)

        mock_smbus2 = MagicMock()
        mock_smbus2.SMBus.return_value = mock_smb

        with patch.dict("sys.modules", {"smbus2": mock_smbus2}):
            import importlib
            import casectl.hardware.detect as detect_mod

            importlib.reload(detect_mod)
            try:
                assert detect_mod.is_oled_present() is True
            finally:
                importlib.reload(detect_mod)

    def test_is_oled_present_false(self) -> None:
        """When smbus2 read raises OSError, return False."""
        mock_smb = MagicMock()
        mock_smb.__enter__ = MagicMock(return_value=mock_smb)
        mock_smb.__exit__ = MagicMock(return_value=False)
        mock_smb.read_byte = MagicMock(side_effect=OSError("No device"))

        mock_smbus2 = MagicMock()
        mock_smbus2.SMBus.return_value = mock_smb

        with patch.dict("sys.modules", {"smbus2": mock_smbus2}):
            import importlib
            import casectl.hardware.detect as detect_mod

            importlib.reload(detect_mod)
            try:
                assert detect_mod.is_oled_present() is False
            finally:
                importlib.reload(detect_mod)


# ---------------------------------------------------------------------------
# get_platform_info
# ---------------------------------------------------------------------------


class TestGetPlatformInfo:
    """Verify platform info collection from sysfs and hostname."""

    def test_get_platform_info_returns_dict(self) -> None:
        """When sysfs files exist, populate all fields correctly."""
        with (
            patch.object(
                Path,
                "read_text",
                side_effect=lambda *args, **kwargs: "Raspberry Pi 5 Model B\x00",
            ),
            patch.object(
                Path, "read_bytes", return_value=b"\x00\xd0\x41\x20"
            ),
            patch("socket.gethostname", return_value="testhost"),
        ):
            info = get_platform_info()

        assert isinstance(info, dict)
        assert "model" in info
        assert "revision" in info
        assert "serial" in info
        assert "hostname" in info
        assert info["hostname"] == "testhost"
        assert info["revision"] == "00d04120"

    def test_get_platform_info_missing_files(self) -> None:
        """When all sysfs reads fail, all values default to empty strings."""
        with (
            patch.object(Path, "read_text", side_effect=FileNotFoundError),
            patch.object(Path, "read_bytes", side_effect=FileNotFoundError),
            patch("socket.gethostname", side_effect=OSError),
        ):
            info = get_platform_info()

        assert info["model"] == ""
        assert info["revision"] == ""
        assert info["serial"] == ""
        assert info["hostname"] == ""


# ---------------------------------------------------------------------------
# check_i2c_permissions
# ---------------------------------------------------------------------------


class TestCheckI2cPermissions:
    """Verify I2C device permission checks."""

    def test_check_i2c_permissions_ok(self) -> None:
        """When /dev/i2c-1 exists and is accessible, return (True, '')."""
        with (
            patch.object(Path, "exists", return_value=True),
            patch("os.access", return_value=True),
        ):
            ok, msg = check_i2c_permissions()
        assert ok is True
        assert msg == ""

    def test_check_i2c_permissions_missing(self) -> None:
        """When /dev/i2c-1 does not exist, return (False, <helpful msg>)."""
        with patch.object(Path, "exists", return_value=False):
            ok, msg = check_i2c_permissions()
        assert ok is False
        assert "does not exist" in msg
        assert "raspi-config" in msg

    def test_check_i2c_permissions_no_access(self) -> None:
        """When /dev/i2c-1 exists but is not accessible, return (False, msg)."""
        with (
            patch.object(Path, "exists", return_value=True),
            patch("os.access", return_value=False),
        ):
            ok, msg = check_i2c_permissions()
        assert ok is False
        assert "not accessible" in msg
        assert "usermod" in msg
