"""System monitor plugin for casectl.

Collects CPU, memory, disk, temperature, and fan metrics every 2 seconds
and emits them on the event bus for other plugins to consume.
"""

from casectl.plugins.monitor.plugin import SystemMonitorPlugin

__all__ = ["SystemMonitorPlugin"]
