"""Shared fixtures for the casectl test suite.

Provides mock objects for hardware, configuration, event bus, and
plugin infrastructure so that tests never touch real I2C, sysfs, or
the filesystem (except via tmp_path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.config.models import CaseCtlConfig, FanConfig, FanMode
from casectl.daemon.event_bus import EventBus
from casectl.plugins.base import HardwareRegistry


# ---------------------------------------------------------------------------
# I2C / SMBus mock
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_smbus() -> MagicMock:
    """Return a MagicMock that mimics an smbus2.SMBus instance.

    The mock has ``write_i2c_block_data``, ``read_i2c_block_data``,
    and ``close`` pre-configured as regular MagicMocks.
    """
    bus = MagicMock()
    bus.write_i2c_block_data = MagicMock()
    bus.read_i2c_block_data = MagicMock(return_value=[0] * 6)
    bus.close = MagicMock()
    return bus


# ---------------------------------------------------------------------------
# ExpansionBoard with mocked I2C
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_expansion(mock_smbus: MagicMock) -> Any:
    """Return an ExpansionBoard whose I2C bus is a mock.

    The smbus2 import is patched so that ``SMBus(1)`` returns *mock_smbus*,
    and the board is ready to use without real hardware.
    """
    mock_smbus2_module = MagicMock()
    mock_smbus2_module.SMBus.return_value = mock_smbus

    with patch.dict("sys.modules", {"smbus2": mock_smbus2_module}):
        # Force re-evaluation of the availability flag
        import casectl.hardware.expansion as exp_mod

        original_available = exp_mod._available
        exp_mod._available = True
        exp_mod.smbus2 = mock_smbus2_module

        from casectl.hardware.expansion import ExpansionBoard

        board = ExpansionBoard.__new__(ExpansionBoard)
        board._bus_number = 1
        board._address = 0x21
        board._bus = mock_smbus
        board._consecutive_errors = 0
        board._degraded = False
        board._closed = False
        board._last_transaction = 0.0
        import threading

        board._i2c_lock = threading.Lock()

        yield board

        exp_mod._available = original_available


# ---------------------------------------------------------------------------
# SystemInfo with mocked sysfs / psutil
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_system_info() -> MagicMock:
    """Return a MagicMock that mimics a SystemInfo instance.

    Pre-configures return values for commonly called methods.
    """
    si = MagicMock()
    si.get_cpu_usage.return_value = 25.0
    si.get_cpu_temperature.return_value = 42.5
    si.get_fan_duty.return_value = 128
    si.get_ip_address.return_value = "192.168.1.100"

    mem = MagicMock()
    mem.percent = 45.0
    mem.used_gb = 2.0
    mem.total_gb = 4.0
    si.get_memory_usage.return_value = mem

    disk = MagicMock()
    disk.percent = 60.0
    disk.used_gb = 30.0
    disk.total_gb = 50.0
    si.get_disk_usage.return_value = disk

    si.get_date.return_value = "2026-03-24"
    si.get_weekday.return_value = "Tuesday"
    si.get_time.return_value = "14:30:00"
    return si


# ---------------------------------------------------------------------------
# ConfigManager with in-memory config (no file I/O)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_config_manager(tmp_path: Path) -> Any:
    """Return a real ConfigManager pointed at a tmp_path file.

    This allows tests to exercise load/save without touching the user's
    real config directory.
    """
    from casectl.config.manager import ConfigManager

    config_file = tmp_path / "casectl" / "config.yaml"
    mgr = ConfigManager(path=config_file)
    return mgr


# ---------------------------------------------------------------------------
# EventBus (real instance — no mocks needed)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_event_bus() -> EventBus:
    """Return a fresh EventBus instance."""
    return EventBus(max_ws=10)


# ---------------------------------------------------------------------------
# HardwareRegistry with mocked hardware
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_hardware_registry(
    mock_expansion: Any,
    mock_system_info: MagicMock,
) -> HardwareRegistry:
    """Return a HardwareRegistry populated with mock hardware objects."""
    return HardwareRegistry(
        expansion=mock_expansion,
        oled=None,  # OLED not needed for most tests
        system_info=mock_system_info,
    )


# ---------------------------------------------------------------------------
# FanController helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fan_config_follow_temp() -> FanConfig:
    """Return a FanConfig in FOLLOW_TEMP mode with known thresholds."""
    return FanConfig(
        mode=FanMode.FOLLOW_TEMP,
        manual_duty=[75, 75, 75],
    )
