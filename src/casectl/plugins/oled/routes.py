"""FastAPI routes for the oled-display plugin.

Mounted at ``/api/plugins/oled-display`` by the plugin host.
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

    Called by :class:`OledDisplayPlugin` during ``setup()``.
    """
    global _get_status, _get_config  # noqa: PLW0603
    _get_status = get_status
    _get_config = get_config


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class OledStatusResponse(BaseModel):
    """Response model for GET /status."""

    current_screen: int = Field(description="Index of the currently displayed screen")
    screen_names: list[str] = Field(description="Names of all available screens")
    screens_enabled: list[bool] = Field(description="Enabled state of each screen")
    rotation: int = Field(description="Display rotation in degrees")
    degraded: bool = Field(description="Whether the display is unavailable")


class SetScreenRequest(BaseModel):
    """Request body for POST /screen."""

    index: int = Field(ge=0, le=3, description="Screen index (0-3)")
    enabled: bool = Field(description="Whether to enable or disable the screen")


class SetRotationRequest(BaseModel):
    """Request body for POST /rotation."""

    rotation: int = Field(description="Display rotation in degrees (0 or 180)")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=OledStatusResponse)
async def oled_status() -> OledStatusResponse:
    """Return the current OLED display state."""
    if _get_status is None:
        raise HTTPException(status_code=503, detail="OLED display not initialised")

    status = _get_status()
    return OledStatusResponse(
        current_screen=status.get("current_screen", 0),
        screen_names=status.get("screen_names", []),
        screens_enabled=status.get("screens_enabled", []),
        rotation=status.get("rotation", 0),
        degraded=status.get("degraded", False),
    )


@router.post("/screen")
async def set_screen(request: SetScreenRequest) -> dict[str, Any]:
    """Enable or disable a specific screen in the rotation cycle."""
    if _get_config is None:
        raise HTTPException(status_code=503, detail="Config manager not available")

    try:
        config_manager = _get_config()
        raw = await config_manager.get("oled")

        # Update the specific screen's enabled state.
        screens = raw.get("screens", [])
        if request.index >= len(screens):
            raise HTTPException(
                status_code=400,
                detail=f"Screen index {request.index} out of range (0-{len(screens) - 1})",
            )

        screens[request.index]["enabled"] = request.enabled
        await config_manager.update("oled", {"screens": screens})
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update OLED screen config", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "status": "ok",
        "index": request.index,
        "enabled": request.enabled,
    }


@router.post("/rotation")
async def set_rotation(request: SetRotationRequest) -> dict[str, Any]:
    """Set the display rotation."""
    if _get_config is None:
        raise HTTPException(status_code=503, detail="Config manager not available")

    if request.rotation not in (0, 180):
        raise HTTPException(
            status_code=400,
            detail=f"Rotation must be 0 or 180, got {request.rotation}",
        )

    try:
        config_manager = _get_config()
        await config_manager.update("oled", {"rotation": request.rotation})
    except Exception as exc:
        logger.error("Failed to update OLED rotation config", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return {"status": "ok", "rotation": request.rotation}
