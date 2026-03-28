"""FastAPI routes for the oled-display plugin.

Mounted at ``/api/plugins/oled-display`` by the plugin host.

Dependencies are injected via ``app.state`` using FastAPI's ``Depends()``
mechanism rather than module-level globals.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from casectl.config.manager import ConfigManager

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency injection helpers
# ---------------------------------------------------------------------------


def _get_oled_status(request: Request) -> dict[str, Any]:
    """Retrieve the OLED status callable from ``app.state`` and return its result.

    Raises :class:`HTTPException` 503 if the status accessor has not been set.
    """
    get_status = getattr(request.app.state, "oled_get_status", None)
    if get_status is None:
        raise HTTPException(status_code=503, detail="OLED display not initialised")
    return get_status()


def _get_oled_config_manager(request: Request) -> ConfigManager:
    """Retrieve the config manager from ``app.state``.

    Raises :class:`HTTPException` 503 if the config manager has not been set.
    """
    config_manager: ConfigManager | None = getattr(request.app.state, "oled_config_manager", None)
    if config_manager is None:
        raise HTTPException(status_code=503, detail="Config manager not available")
    return config_manager


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
    """Request body for PUT /screen."""

    index: int = Field(ge=0, le=3, description="Screen index (0-3)")
    enabled: bool = Field(description="Whether to enable or disable the screen")


class SetRotationRequest(BaseModel):
    """Request body for PUT /rotation."""

    rotation: Literal[0, 180] = Field(description="Display rotation in degrees (0 or 180)")


class SetPowerRequest(BaseModel):
    """Request body for PUT /power."""

    enabled: bool = Field(description="Whether to enable or disable all screens")


class SetContentRequest(BaseModel):
    """Request body for PUT /content."""

    screen_index: int = Field(ge=0, le=3, description="Screen index (0-3)")
    display_time: float | None = Field(
        default=None, gt=0, description="Seconds to display this screen"
    )
    time_format: Literal[0, 1] | None = Field(
        default=None, description="Time format: 0=24h, 1=12h"
    )
    date_format: int | None = Field(
        default=None, ge=0, description="Date display variant"
    )
    interchange: int | None = Field(
        default=None, ge=0, description="Additional display option"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=OledStatusResponse)
async def oled_status(
    status: Annotated[dict[str, Any], Depends(_get_oled_status)],
) -> OledStatusResponse:
    """Return the current OLED display state."""
    return OledStatusResponse(
        current_screen=status.get("current_screen", 0),
        screen_names=status.get("screen_names", []),
        screens_enabled=status.get("screens_enabled", []),
        rotation=status.get("rotation", 0),
        degraded=status.get("degraded", False),
    )


@router.put("/screen")
async def set_screen(
    request: SetScreenRequest,
    config_manager: Annotated[Any, Depends(_get_oled_config_manager)],
) -> dict[str, Any]:
    """Enable or disable a specific screen in the rotation cycle."""
    try:
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
        raise HTTPException(status_code=500, detail="Internal server error")

    return {
        "status": "ok",
        "index": request.index,
        "enabled": request.enabled,
    }


@router.put("/rotation")
async def set_rotation(
    request: SetRotationRequest,
    config_manager: Annotated[Any, Depends(_get_oled_config_manager)],
) -> dict[str, Any]:
    """Set the display rotation."""
    try:
        await config_manager.update("oled", {"rotation": request.rotation})
    except Exception as exc:
        logger.error("Failed to update OLED rotation config", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"status": "ok", "rotation": request.rotation}


@router.put("/power")
async def set_power(
    request: SetPowerRequest,
    config_manager: Annotated[Any, Depends(_get_oled_config_manager)],
) -> dict[str, Any]:
    """Enable or disable all OLED screens at once (power on/off).

    Matches CLI ``casectl oled on`` / ``casectl oled off`` behaviour.
    """
    try:
        raw = await config_manager.get("oled")
        screens = raw.get("screens", [])
        for screen in screens:
            screen["enabled"] = request.enabled
        await config_manager.update("oled", {"screens": screens})
    except Exception:
        logger.error("Failed to update OLED power state", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"status": "ok", "enabled": request.enabled}


@router.put("/content")
async def set_content(
    request: SetContentRequest,
    config_manager: Annotated[Any, Depends(_get_oled_config_manager)],
) -> dict[str, Any]:
    """Update per-screen content settings (display time, time format, etc.).

    Only fields present in the request body are modified; unset fields
    are left unchanged.
    """
    try:
        raw = await config_manager.get("oled")
        screens = raw.get("screens", [])
        if request.screen_index >= len(screens):
            raise HTTPException(
                status_code=400,
                detail=f"Screen index {request.screen_index} out of range (0-{len(screens) - 1})",
            )

        screen = dict(screens[request.screen_index])
        if request.display_time is not None:
            screen["display_time"] = request.display_time
        if request.time_format is not None:
            screen["time_format"] = request.time_format
        if request.date_format is not None:
            screen["date_format"] = request.date_format
        if request.interchange is not None:
            screen["interchange"] = request.interchange

        screens[request.screen_index] = screen
        await config_manager.update("oled", {"screens": screens})
    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to update OLED content settings", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {
        "status": "ok",
        "screen_index": request.screen_index,
        "settings": screen,
    }
