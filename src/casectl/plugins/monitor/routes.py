"""FastAPI routes for the system-monitor plugin.

Mounted at ``/api/plugins/system-monitor`` by the plugin host.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level reference — set by the plugin during setup.
# ---------------------------------------------------------------------------

_get_metrics: Any = None  # callable returning latest metrics dict


def configure(get_metrics: Any) -> None:
    """Wire the metrics accessor into the route module.

    Called by :class:`SystemMonitorPlugin` during ``setup()``.
    """
    global _get_metrics  # noqa: PLW0603
    _get_metrics = get_metrics


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status")
async def monitor_status() -> dict[str, Any]:
    """Return the full set of latest system metrics as JSON.

    The response schema matches :class:`casectl.config.models.SystemMetrics`.
    """
    if _get_metrics is None:
        raise HTTPException(status_code=503, detail="System monitor not initialised")

    metrics = _get_metrics()
    if metrics is None:
        raise HTTPException(status_code=503, detail="No metrics collected yet")

    return metrics
