"""OLED display plugin for casectl.

Drives the SSD1306 128x64 OLED display, cycling through configurable screens
showing clock, system metrics, temperatures, and fan duty.
"""

from casectl.plugins.oled.plugin import OledDisplayPlugin

__all__ = ["OledDisplayPlugin"]
