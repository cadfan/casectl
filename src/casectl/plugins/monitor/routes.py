"""FastAPI routes for the system-monitor plugin.

Mounted at ``/api/plugins/system-monitor`` by the plugin host.

Dependencies are injected via ``app.state`` using FastAPI's ``Depends()``
mechanism rather than module-level globals.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency injection helpers
# ---------------------------------------------------------------------------


def _get_monitor_metrics(request: Request) -> dict[str, Any] | None:
    """Retrieve the metrics accessor from ``app.state`` and return its result.

    Raises :class:`HTTPException` 503 if the accessor has not been set.
    """
    get_metrics = getattr(request.app.state, "monitor_get_metrics", None)
    if get_metrics is None:
        raise HTTPException(status_code=503, detail="System monitor not initialised")
    return get_metrics()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status")
async def monitor_status(
    metrics: Annotated[dict[str, Any] | None, Depends(_get_monitor_metrics)],
) -> dict[str, Any]:
    """Return the full set of latest system metrics as JSON.

    The response schema matches :class:`casectl.config.models.SystemMetrics`.
    """
    if metrics is None:
        raise HTTPException(status_code=503, detail="No metrics collected yet")

    return metrics
