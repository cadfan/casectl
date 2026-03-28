"""Event-driven automation rules engine for casectl.

Provides declarative YAML-based rules that react to EventBus events and
execute actions (fan speed, LED mode, alerts, etc.) with priority-based
conflict resolution: safety > scheduled > user.
"""

from casectl.plugins.automation.plugin import AutomationPlugin

__all__ = ["AutomationPlugin"]
