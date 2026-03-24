"""System metrics collection using psutil and sysfs.

Provides safe, non-throwing accessors for CPU, memory, disk, network,
fan, and time information on Raspberry Pi hosts.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# sysfs paths
# ---------------------------------------------------------------------------
THERMAL_ZONE_PATH = Path("/sys/devices/virtual/thermal/thermal_zone0/temp")
COOLING_FAN_HWMON_BASE = Path("/sys/devices/platform/cooling_fan/hwmon")


# ---------------------------------------------------------------------------
# Dataclasses for structured metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryInfo:
    """System memory usage snapshot."""

    percent: float
    used_gb: float
    total_gb: float


@dataclass(frozen=True, slots=True)
class DiskInfo:
    """Root filesystem disk usage snapshot."""

    percent: float
    used_gb: float
    total_gb: float


@dataclass(frozen=True, slots=True)
class AllMetrics:
    """Aggregated snapshot of all system metrics."""

    cpu_usage: float
    cpu_temperature: float
    memory: MemoryInfo
    disk: DiskInfo
    ip_address: str
    fan_duty: int
    date: str
    weekday: str
    time: str


# ---------------------------------------------------------------------------
# SystemInfo
# ---------------------------------------------------------------------------


class SystemInfo:
    """Non-throwing system metrics collector.

    Every public method returns a safe default on failure (``0.0``, ``"N/A"``,
    etc.) and never raises an exception.
    """

    def __init__(self) -> None:
        self._hwmon_pwm_path: Path | None = None

    # ------------------------------------------------------------------
    # CPU
    # ------------------------------------------------------------------

    def get_cpu_usage(self) -> float:
        """Return overall CPU usage as a percentage (0-100)."""
        try:
            return psutil.cpu_percent(interval=None)
        except Exception:
            logger.debug("Failed to read CPU usage", exc_info=True)
            return 0.0

    def get_cpu_temperature(self) -> float:
        """Return CPU temperature in degrees Celsius from sysfs thermal zone."""
        try:
            raw = THERMAL_ZONE_PATH.read_text().strip()
            return int(raw) / 1000.0
        except Exception:
            logger.debug("Failed to read CPU temperature", exc_info=True)
            return 0.0

    # ------------------------------------------------------------------
    # Memory / Disk
    # ------------------------------------------------------------------

    def get_memory_usage(self) -> MemoryInfo:
        """Return current memory usage."""
        try:
            mem = psutil.virtual_memory()
            return MemoryInfo(
                percent=mem.percent,
                used_gb=round(mem.used / (1024**3), 2),
                total_gb=round(mem.total / (1024**3), 2),
            )
        except Exception:
            logger.debug("Failed to read memory usage", exc_info=True)
            return MemoryInfo(percent=0.0, used_gb=0.0, total_gb=0.0)

    def get_disk_usage(self) -> DiskInfo:
        """Return root filesystem disk usage."""
        try:
            disk = psutil.disk_usage("/")
            return DiskInfo(
                percent=disk.percent,
                used_gb=round(disk.used / (1024**3), 2),
                total_gb=round(disk.total / (1024**3), 2),
            )
        except Exception:
            logger.debug("Failed to read disk usage", exc_info=True)
            return DiskInfo(percent=0.0, used_gb=0.0, total_gb=0.0)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def get_ip_address(self) -> str:
        """Return the primary local IP address, or ``'N/A'`` on failure.

        Uses a non-connecting UDP socket to determine which interface would
        route to an external address.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except Exception:
            logger.debug("Failed to determine IP address", exc_info=True)
            return "N/A"

    # ------------------------------------------------------------------
    # Fan (kernel PWM via sysfs)
    # ------------------------------------------------------------------

    def _resolve_hwmon_pwm_path(self) -> Path | None:
        """Locate the pwm1 file under the cooling_fan hwmon directory.

        The hwmon index (``hwmonX``) is not stable across reboots, so we
        search for it on the first call and cache the result.
        """
        if self._hwmon_pwm_path is not None:
            return self._hwmon_pwm_path

        try:
            if not COOLING_FAN_HWMON_BASE.is_dir():
                return None
            for entry in COOLING_FAN_HWMON_BASE.iterdir():
                candidate = entry / "pwm1"
                if candidate.is_file():
                    self._hwmon_pwm_path = candidate
                    logger.debug("Resolved fan PWM path: %s", candidate)
                    return candidate
        except Exception:
            logger.debug("Failed to resolve hwmon PWM path", exc_info=True)
        return None

    def get_fan_duty(self) -> int:
        """Return the kernel-controlled fan PWM duty (0-255), or ``0`` on failure."""
        try:
            pwm_path = self._resolve_hwmon_pwm_path()
            if pwm_path is None:
                return 0
            raw = pwm_path.read_text().strip()
            return int(raw)
        except Exception:
            logger.debug("Failed to read fan duty", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Date / time helpers
    # ------------------------------------------------------------------

    def get_date(self) -> str:
        """Return the current date as ``YYYY-MM-DD``."""
        try:
            return datetime.now().strftime("%Y-%m-%d")
        except Exception:
            return "N/A"

    def get_weekday(self) -> str:
        """Return the current weekday name (e.g. ``Monday``)."""
        try:
            return datetime.now().strftime("%A")
        except Exception:
            return "N/A"

    def get_time(self) -> str:
        """Return the current time as ``HH:MM:SS``."""
        try:
            return datetime.now().strftime("%H:%M:%S")
        except Exception:
            return "N/A"

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------

    def get_all_metrics(self) -> AllMetrics:
        """Collect all metrics into a single snapshot.

        Individual failures are silently replaced with safe defaults so that
        the overall call never raises.
        """
        return AllMetrics(
            cpu_usage=self.get_cpu_usage(),
            cpu_temperature=self.get_cpu_temperature(),
            memory=self.get_memory_usage(),
            disk=self.get_disk_usage(),
            ip_address=self.get_ip_address(),
            fan_duty=self.get_fan_duty(),
            date=self.get_date(),
            weekday=self.get_weekday(),
            time=self.get_time(),
        )
