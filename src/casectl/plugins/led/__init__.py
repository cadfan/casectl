"""LED control plugin for casectl.

Manages the RGB LEDs on the STM32 expansion board with support for rainbow,
breathing, follow-temperature, manual colour, and off modes.
"""

from casectl.plugins.led.plugin import LedControlPlugin

__all__ = ["LedControlPlugin"]
