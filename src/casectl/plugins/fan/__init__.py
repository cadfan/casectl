"""Fan control plugin for casectl.

Manages the three STM32-driven case fans with support for temperature-based
automatic control, Raspberry Pi fan following, manual duty, and off modes.
"""

from casectl.plugins.fan.plugin import FanControlPlugin

__all__ = ["FanControlPlugin"]
