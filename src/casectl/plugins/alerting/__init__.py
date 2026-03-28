"""Alerting plugin for casectl — webhook, ntfy.sh, and SMTP channels.

Provides three alerting channels that can be triggered by automation rules
via the ``alert`` action target.  Each channel operates independently and
is enabled when its required configuration fields are set.
"""

from casectl.plugins.alerting.plugin import AlertingPlugin

__all__ = ["AlertingPlugin"]
