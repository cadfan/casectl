"""FastAPI routes for the MQTT plugin.

Mounted at ``/api/plugins/mqtt`` by the plugin host.  Provides status
endpoints for the MQTT connection, device state manager, metric publisher,
and Home Assistant discovery.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_mqtt_manager(request: Request) -> Any:
    """Retrieve the MqttConnectionManager from ``app.state``."""
    mgr = getattr(request.app.state, "mqtt_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="MQTT plugin not initialised")
    return mgr


def _get_state_manager(request: Request) -> Any:
    """Retrieve the DeviceStateManager from ``app.state``."""
    mgr = getattr(request.app.state, "mqtt_state_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="MQTT state manager not initialised")
    return mgr


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status")
async def get_mqtt_status(request: Request) -> dict[str, Any]:
    """Return the overall MQTT plugin status.

    Combines connection status, device state manager stats, metric publisher
    stats, and HA discovery status into a single response.
    """
    result: dict[str, Any] = {}

    mqtt_mgr = getattr(request.app.state, "mqtt_manager", None)
    if mqtt_mgr is not None:
        result["connection"] = mqtt_mgr.get_status()

    state_mgr = getattr(request.app.state, "mqtt_state_manager", None)
    if state_mgr is not None:
        result["state_manager"] = state_mgr.get_status()

    metric_pub = getattr(request.app.state, "mqtt_metric_publisher", None)
    if metric_pub is not None:
        result["metric_publisher"] = metric_pub.get_status()

    ha_disc = getattr(request.app.state, "mqtt_ha_discovery", None)
    if ha_disc is not None:
        result["ha_discovery"] = ha_disc.get_status()

    if not result:
        raise HTTPException(status_code=503, detail="MQTT plugin not initialised")

    return result


@router.get("/connection")
async def get_connection_status(request: Request) -> dict[str, Any]:
    """Return MQTT connection status."""
    mgr = _get_mqtt_manager(request)
    return mgr.get_status()


@router.get("/devices")
async def get_device_state_status(request: Request) -> dict[str, Any]:
    """Return device state manager status including publish/command counts."""
    mgr = _get_state_manager(request)
    return mgr.get_status()
