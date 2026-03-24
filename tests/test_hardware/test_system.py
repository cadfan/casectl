"""Tests for casectl.hardware.system — SystemInfo metrics collector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from casectl.hardware.system import (
    AllMetrics,
    DiskInfo,
    MemoryInfo,
    SystemInfo,
    THERMAL_ZONE_PATH,
    COOLING_FAN_HWMON_BASE,
)


# ---------------------------------------------------------------------------
# CPU temperature
# ---------------------------------------------------------------------------


class TestGetCpuTemperature:
    """Verify CPU temperature reading from sysfs thermal zone."""

    def test_get_cpu_temperature_millidegrees_conversion(self) -> None:
        """Sysfs reports millidegrees; verify conversion to float degrees C."""
        si = SystemInfo()
        with patch.object(THERMAL_ZONE_PATH, "read_text", return_value="48250\n"):
            temp = si.get_cpu_temperature()
        assert temp == pytest.approx(48.25)

    def test_get_cpu_temperature_exact_integer(self) -> None:
        """42000 millidegrees -> 42.0 C."""
        si = SystemInfo()
        with patch.object(THERMAL_ZONE_PATH, "read_text", return_value="42000"):
            temp = si.get_cpu_temperature()
        assert temp == 42.0

    def test_get_cpu_temperature_missing_file(self) -> None:
        """If the sysfs file does not exist, returns 0.0."""
        si = SystemInfo()
        with patch.object(THERMAL_ZONE_PATH, "read_text", side_effect=FileNotFoundError):
            temp = si.get_cpu_temperature()
        assert temp == 0.0

    def test_get_cpu_temperature_permission_error(self) -> None:
        """Permission denied on sysfs file returns 0.0."""
        si = SystemInfo()
        with patch.object(THERMAL_ZONE_PATH, "read_text", side_effect=PermissionError):
            temp = si.get_cpu_temperature()
        assert temp == 0.0

    def test_get_cpu_temperature_corrupt_content(self) -> None:
        """Non-integer content in sysfs returns 0.0."""
        si = SystemInfo()
        with patch.object(THERMAL_ZONE_PATH, "read_text", return_value="not_a_number"):
            temp = si.get_cpu_temperature()
        assert temp == 0.0


# ---------------------------------------------------------------------------
# Memory usage
# ---------------------------------------------------------------------------


class TestGetMemoryUsage:
    """Verify memory usage reads from psutil."""

    def test_get_memory_usage(self) -> None:
        """Mock psutil.virtual_memory and verify MemoryInfo fields."""
        mock_mem = MagicMock()
        mock_mem.percent = 65.3
        mock_mem.used = 4 * (1024**3)  # 4 GB
        mock_mem.total = 8 * (1024**3)  # 8 GB

        si = SystemInfo()
        with patch("casectl.hardware.system.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            info = si.get_memory_usage()

        assert isinstance(info, MemoryInfo)
        assert info.percent == 65.3
        assert info.used_gb == 4.0
        assert info.total_gb == 8.0

    def test_get_memory_usage_on_error(self) -> None:
        """If psutil raises, returns zeroed MemoryInfo."""
        si = SystemInfo()
        with patch("casectl.hardware.system.psutil") as mock_psutil:
            mock_psutil.virtual_memory.side_effect = RuntimeError("psutil error")
            info = si.get_memory_usage()

        assert info.percent == 0.0
        assert info.used_gb == 0.0
        assert info.total_gb == 0.0

    def test_get_memory_usage_rounding(self) -> None:
        """Verify GB values are rounded to 2 decimal places."""
        mock_mem = MagicMock()
        mock_mem.percent = 50.0
        mock_mem.used = int(3.456789 * (1024**3))
        mock_mem.total = int(7.891234 * (1024**3))

        si = SystemInfo()
        with patch("casectl.hardware.system.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            info = si.get_memory_usage()

        assert info.used_gb == round(mock_mem.used / (1024**3), 2)
        assert info.total_gb == round(mock_mem.total / (1024**3), 2)


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------


class TestGetDiskUsage:
    """Verify disk usage reads from psutil."""

    def test_get_disk_usage(self) -> None:
        mock_disk = MagicMock()
        mock_disk.percent = 72.1
        mock_disk.used = 100 * (1024**3)
        mock_disk.total = 256 * (1024**3)

        si = SystemInfo()
        with patch("casectl.hardware.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = mock_disk
            info = si.get_disk_usage()

        assert isinstance(info, DiskInfo)
        assert info.percent == 72.1
        assert info.used_gb == 100.0
        assert info.total_gb == 256.0

    def test_get_disk_usage_on_error(self) -> None:
        si = SystemInfo()
        with patch("casectl.hardware.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.side_effect = OSError("No such device")
            info = si.get_disk_usage()
        assert info.percent == 0.0


# ---------------------------------------------------------------------------
# Fan duty (sysfs hwmon)
# ---------------------------------------------------------------------------


class TestGetFanDuty:
    """Verify fan duty resolution from the cooling_fan hwmon directory."""

    def test_get_fan_duty_with_resolved_path(self, tmp_path: Path) -> None:
        """When hwmon path exists and contains pwm1, read the value."""
        hwmon_dir = tmp_path / "hwmon0"
        hwmon_dir.mkdir()
        pwm_file = hwmon_dir / "pwm1"
        pwm_file.write_text("178\n")

        si = SystemInfo()
        # Pre-cache the resolved path
        si._hwmon_pwm_path = pwm_file
        duty = si.get_fan_duty()
        assert duty == 178

    def test_get_fan_duty_no_hwmon_dir(self) -> None:
        """When cooling_fan hwmon base does not exist, returns 0."""
        si = SystemInfo()
        with patch.object(COOLING_FAN_HWMON_BASE, "is_dir", return_value=False):
            duty = si.get_fan_duty()
        assert duty == 0

    def test_get_fan_duty_resolves_dynamically(self, tmp_path: Path) -> None:
        """First call resolves hwmonX/pwm1 dynamically and caches it."""
        hwmon_dir = tmp_path / "hwmon3"
        hwmon_dir.mkdir()
        pwm_file = hwmon_dir / "pwm1"
        pwm_file.write_text("200\n")

        si = SystemInfo()
        with (
            patch.object(COOLING_FAN_HWMON_BASE, "is_dir", return_value=True),
            patch.object(COOLING_FAN_HWMON_BASE, "iterdir", return_value=[hwmon_dir]),
        ):
            duty = si.get_fan_duty()

        assert duty == 200
        # Verify the path was cached
        assert si._hwmon_pwm_path == pwm_file

    def test_get_fan_duty_cached_path(self, tmp_path: Path) -> None:
        """Second call uses the cached path without re-resolving."""
        pwm_file = tmp_path / "pwm1"
        pwm_file.write_text("150\n")

        si = SystemInfo()
        si._hwmon_pwm_path = pwm_file

        # No need to patch COOLING_FAN_HWMON_BASE — cache bypasses it
        duty = si.get_fan_duty()
        assert duty == 150


# ---------------------------------------------------------------------------
# CPU usage
# ---------------------------------------------------------------------------


class TestGetCpuUsage:
    """Verify CPU usage reads from psutil."""

    def test_get_cpu_usage(self) -> None:
        si = SystemInfo()
        with patch("casectl.hardware.system.psutil") as mock_psutil:
            mock_psutil.cpu_percent.return_value = 35.7
            usage = si.get_cpu_usage()
        assert usage == 35.7

    def test_get_cpu_usage_on_error(self) -> None:
        si = SystemInfo()
        with patch("casectl.hardware.system.psutil") as mock_psutil:
            mock_psutil.cpu_percent.side_effect = RuntimeError("psutil error")
            usage = si.get_cpu_usage()
        assert usage == 0.0


# ---------------------------------------------------------------------------
# IP address
# ---------------------------------------------------------------------------


class TestGetIpAddress:
    """Verify IP address detection."""

    def test_get_ip_address(self) -> None:
        si = SystemInfo()
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.getsockname.return_value = ("10.0.0.42", 0)

        with patch("casectl.hardware.system.socket.socket", return_value=mock_sock):
            ip = si.get_ip_address()
        assert ip == "10.0.0.42"

    def test_get_ip_address_failure(self) -> None:
        si = SystemInfo()
        with patch("casectl.hardware.system.socket.socket", side_effect=OSError):
            ip = si.get_ip_address()
        assert ip == "N/A"


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


class TestGetAllMetrics:
    """Verify get_all_metrics aggregates with partial failures."""

    def test_get_all_metrics_success(self) -> None:
        si = SystemInfo()
        with (
            patch.object(si, "get_cpu_usage", return_value=25.0),
            patch.object(si, "get_cpu_temperature", return_value=45.0),
            patch.object(
                si,
                "get_memory_usage",
                return_value=MemoryInfo(percent=50.0, used_gb=2.0, total_gb=4.0),
            ),
            patch.object(
                si,
                "get_disk_usage",
                return_value=DiskInfo(percent=60.0, used_gb=30.0, total_gb=50.0),
            ),
            patch.object(si, "get_ip_address", return_value="192.168.1.1"),
            patch.object(si, "get_fan_duty", return_value=128),
            patch.object(si, "get_date", return_value="2026-03-24"),
            patch.object(si, "get_weekday", return_value="Tuesday"),
            patch.object(si, "get_time", return_value="14:00:00"),
        ):
            metrics = si.get_all_metrics()

        assert isinstance(metrics, AllMetrics)
        assert metrics.cpu_usage == 25.0
        assert metrics.cpu_temperature == 45.0
        assert metrics.memory.percent == 50.0
        assert metrics.disk.total_gb == 50.0
        assert metrics.ip_address == "192.168.1.1"
        assert metrics.fan_duty == 128
        assert metrics.date == "2026-03-24"

    def test_get_all_metrics_with_partial_failure(self) -> None:
        """If some sub-calls fail, the aggregate still returns defaults."""
        si = SystemInfo()
        with (
            patch.object(si, "get_cpu_usage", return_value=0.0),
            patch.object(si, "get_cpu_temperature", return_value=0.0),
            patch.object(
                si,
                "get_memory_usage",
                return_value=MemoryInfo(percent=0.0, used_gb=0.0, total_gb=0.0),
            ),
            patch.object(
                si,
                "get_disk_usage",
                return_value=DiskInfo(percent=0.0, used_gb=0.0, total_gb=0.0),
            ),
            patch.object(si, "get_ip_address", return_value="N/A"),
            patch.object(si, "get_fan_duty", return_value=0),
            patch.object(si, "get_date", return_value="N/A"),
            patch.object(si, "get_weekday", return_value="N/A"),
            patch.object(si, "get_time", return_value="N/A"),
        ):
            metrics = si.get_all_metrics()

        # Should not raise — all fields get safe defaults
        assert metrics.cpu_usage == 0.0
        assert metrics.ip_address == "N/A"

    def test_all_metrics_is_frozen_dataclass(self) -> None:
        """AllMetrics instances should be immutable."""
        metrics = AllMetrics(
            cpu_usage=10.0,
            cpu_temperature=40.0,
            memory=MemoryInfo(percent=50.0, used_gb=2.0, total_gb=4.0),
            disk=DiskInfo(percent=30.0, used_gb=10.0, total_gb=32.0),
            ip_address="10.0.0.1",
            fan_duty=100,
            date="2026-01-01",
            weekday="Thursday",
            time="12:00:00",
        )
        with pytest.raises(AttributeError):
            metrics.cpu_usage = 99.9  # type: ignore[misc]
