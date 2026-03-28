"""FastAPI routes for the led-control plugin.

Mounted at ``/api/plugins/led-control`` by the plugin host.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

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


_COLOR_NAMES: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "orange": (255, 165, 0),
    "pink": (255, 105, 180),
    "purple": (128, 0, 128),
    "teal": (0, 128, 128),
    "coral": (255, 127, 80),
    "gold": (255, 215, 0),
    "lime": (0, 255, 0),
    "navy": (0, 0, 128),
    "arctic-steel": (138, 170, 196),
}

# Reverse lookup: (r, g, b) -> name
_RGB_TO_NAME: dict[tuple[int, int, int], str] = {v: k for k, v in _COLOR_NAMES.items()}


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB values to a hex colour string."""
    return f"#{r:02X}{g:02X}{b:02X}"


def _rgb_to_name(r: int, g: int, b: int) -> str | None:
    """Return the named colour if RGB matches, else None."""
    return _RGB_TO_NAME.get((r, g, b))


class LedStatusResponse(BaseModel):
    """Response model for GET /status."""

    mode: str = Field(description="Current LED mode name")
    color: dict[str, int] = Field(description="Current RGB colour values")
    hex: str = Field(description="Hex colour code (e.g. #FF0080)")
    color_name: str | None = Field(default=None, description="Named colour if one matches")
    degraded: bool = Field(description="Whether the controller is degraded")


class SetLedModeRequest(BaseModel):
    """Request body for POST /mode."""

    mode: int | str = Field(description="LED mode (0-5 or name: rainbow, breathing, follow-temp, manual, custom, off)")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if isinstance(v, str):
            names = {"rainbow": 0, "breathing": 1, "follow-temp": 2, "follow_temp": 2, "manual": 3, "custom": 4, "off": 5}
            if v.lower() in names:
                return names[v.lower()]
            raise ValueError(f"Unknown mode: {v}. Valid: {', '.join(names)}")
        return v


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
    color = status.get("color", {"red": 0, "green": 0, "blue": 0})
    r, g, b = color.get("red", 0), color.get("green", 0), color.get("blue", 0)
    return LedStatusResponse(
        mode=status.get("mode", "unknown"),
        color=color,
        hex=_rgb_to_hex(r, g, b),
        color_name=_rgb_to_name(r, g, b),
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
        raise HTTPException(status_code=500, detail="Internal server error")

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
        raise HTTPException(status_code=500, detail="Internal server error")

    return {
        "status": "ok",
        "color": {"red": request.red, "green": request.green, "blue": request.blue},
    }
