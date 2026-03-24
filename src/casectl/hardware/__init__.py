"""Hardware abstraction layer for casectl.

Re-exports the core hardware drivers and detection utilities so that
consumers can simply ``from casectl.hardware import ...``.
"""

from casectl.hardware.detect import (
    check_i2c_permissions,
    get_platform_info,
    is_case_hardware_present,
    is_oled_present,
    is_raspberry_pi,
)
from casectl.hardware.expansion import ExpansionBoard, FanHwMode, LedHwMode
from casectl.hardware.oled import OledDevice
from casectl.hardware.system import AllMetrics, DiskInfo, MemoryInfo, SystemInfo

__all__ = [
    # Drivers
    "ExpansionBoard",
    "FanHwMode",
    "LedHwMode",
    "OledDevice",
    "SystemInfo",
    # Dataclasses
    "AllMetrics",
    "DiskInfo",
    "MemoryInfo",
    # Detection
    "check_i2c_permissions",
    "get_platform_info",
    "is_case_hardware_present",
    "is_oled_present",
    "is_raspberry_pi",
]
