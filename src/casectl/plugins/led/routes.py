"""FastAPI routes for the led-control plugin.

Mounted at ``/api/plugins/led-control`` by the plugin host.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level references — set by the plugin during setup.
# ---------------------------------------------------------------------------

_get_status: Any = None   # callable returning status dict
_get_config: Any = None   # callable returning config manager


def configure(get_status: Any, get_config: Any) -> None:
    """Wire the status accessor and config manager into the route module.

    Called by :class:`LedControlPlugin` during ``setup()``.
    """
    global _get_status, _get_config  # noqa: PLW0603
    _get_status = get_status
    _get_config = get_config


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LedStatusResponse(BaseModel):
    """Response model for GET /status."""

    mode: str = Field(description="Current LED mode name")
    color: dict[str, int] = Field(description="Current RGB colour values")
    degraded: bool = Field(description="Whether the controller is degraded")


class SetLedModeRequest(BaseModel):
    """Request body for POST /mode."""

    mode: int = Field(description="LED mode enum value (0-5)")


class SetLedColorRequest(BaseModel):
    """Request body for POST /color."""

    red: int = Field(ge=0, le=255, description="Red channel (0-255)")
    green: int = Field(ge=0, le=255, description="Green channel (0-255)")
    blue: int = Field(ge=0, le=255, description="Blue channel (0-255)")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=LedStatusResponse)
async def led_status() -> LedStatusResponse:
    """Return the current LED mode, colour, and health state."""
    if _get_status is None:
        raise HTTPException(status_code=503, detail="LED controller not initialised")

    status = _get_status()
    return LedStatusResponse(
        mode=status.get("mode", "unknown"),
        color=status.get("color", {"red": 0, "green": 0, "blue": 0}),
        degraded=status.get("degraded", False),
    )


@router.post("/mode")
async def set_led_mode(request: SetLedModeRequest) -> dict[str, str]:
    """Set the LED operating mode.

    Persists the new mode to config so it survives daemon restarts.
    """
    if _get_config is None:
        raise HTTPException(status_code=503, detail="Config manager not available")

    from casectl.config.models import LedMode

    try:
        LedMode(request.mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid LED mode: {request.mode}. Valid values: {[m.value for m in LedMode]}",
        )

    try:
        config_manager = _get_config()
        await config_manager.update("led", {"mode": request.mode})
    except Exception as exc:
        logger.error("Failed to update LED mode config", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return {"status": "ok", "mode": LedMode(request.mode).name.lower()}


@router.post("/color")
async def set_led_color(request: SetLedColorRequest) -> dict[str, Any]:
    """Set the LED colour and switch to MANUAL mode.

    The colour is applied immediately and persisted to config.
    """
    if _get_config is None:
        raise HTTPException(status_code=503, detail="Config manager not available")

    from casectl.config.models import LedMode

    try:
        config_manager = _get_config()
        await config_manager.update("led", {
            "mode": LedMode.MANUAL.value,
            "red_value": request.red,
            "green_value": request.green,
            "blue_value": request.blue,
        })
    except Exception as exc:
        logger.error("Failed to update LED colour config", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "status": "ok",
        "color": {"red": request.red, "green": request.green, "blue": request.blue},
    }
