"""casectl plugin system — protocol, context, and supporting types.

All casectl features are plugins.  This package provides the base protocol and
context that every plugin depends on, plus the built-in plugin sub-packages.
"""

from casectl.plugins.base import CasePlugin, HardwareRegistry, PluginContext, PluginStatus

__all__ = [
    "CasePlugin",
    "HardwareRegistry",
    "PluginContext",
    "PluginStatus",
]
