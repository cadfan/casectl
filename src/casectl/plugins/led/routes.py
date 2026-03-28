"""FastAPI routes for the led-control plugin.

Mounted at ``/api/plugins/led-control`` by the plugin host.

Dependencies are injected via ``app.state`` using FastAPI's ``Depends()``
mechanism rather than module-level globals.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from casectl.config.manager import ConfigManager

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency injection helpers
# ---------------------------------------------------------------------------


def _get_led_status(request: Request) -> dict[str, Any]:
    """Retrieve the LED status callable from ``app.state`` and return its result.

    Raises :class:`HTTPException` 503 if the status accessor has not been set.
    """
    get_status = getattr(request.app.state, "led_get_status", None)
    if get_status is None:
        raise HTTPException(status_code=503, detail="LED controller not initialised")
    return get_status()


def _get_led_config_manager(request: Request) -> ConfigManager:
    """Retrieve the config manager from ``app.state``.

    Raises :class:`HTTPException` 503 if the config manager has not been set.
    """
    config_manager: ConfigManager | None = getattr(request.app.state, "led_config_manager", None)
    if config_manager is None:
        raise HTTPException(status_code=503, detail="Config manager not available")
    return config_manager


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
    """Request body for PUT /mode."""

    mode: int | str = Field(description="LED mode (0-5 or name: rainbow, breathing, follow-temp, manual, custom, off)")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        names = {"rainbow": 0, "breathing": 1, "follow-temp": 2, "follow_temp": 2, "manual": 3, "custom": 4, "off": 5}
        if isinstance(v, str):
            if v.lower() in names:
                return names[v.lower()]
            raise ValueError(f"Unknown mode: {v}. Valid: {', '.join(names)}")
        if isinstance(v, int) and v not in range(6):
            raise ValueError(f"Mode must be 0-5, got {v}")
        return v


class SetLedColorRequest(BaseModel):
    """Request body for PUT /color."""

    red: int = Field(ge=0, le=255, description="Red channel (0-255)")
    green: int = Field(ge=0, le=255, description="Green channel (0-255)")
    blue: int = Field(ge=0, le=255, description="Blue channel (0-255)")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=LedStatusResponse)
async def led_status(
    status: Annotated[dict[str, Any], Depends(_get_led_status)],
) -> LedStatusResponse:
    """Return the current LED mode, colour, and health state."""
    color = status.get("color", {"red": 0, "green": 0, "blue": 0})
    r, g, b = color.get("red", 0), color.get("green", 0), color.get("blue", 0)
    return LedStatusResponse(
        mode=status.get("mode", "unknown"),
        color=color,
        hex=_rgb_to_hex(r, g, b),
        color_name=_rgb_to_name(r, g, b),
        degraded=status.get("degraded", False),
    )


@router.put("/mode")
async def set_led_mode(
    request: SetLedModeRequest,
    config_manager: Annotated[Any, Depends(_get_led_config_manager)],
) -> dict[str, str]:
    """Set the LED operating mode.

    Persists the new mode to config so it survives daemon restarts.
    """
    from casectl.config.models import LedMode

    try:
        LedMode(request.mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid LED mode: {request.mode}. Valid values: {[m.value for m in LedMode]}",
        )

    try:
        await config_manager.update("led", {"mode": request.mode})
    except Exception as exc:
        logger.error("Failed to update LED mode config", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"status": "ok", "mode": LedMode(request.mode).name.lower()}


@router.put("/color")
async def set_led_color(
    request: SetLedColorRequest,
    config_manager: Annotated[Any, Depends(_get_led_config_manager)],
) -> dict[str, Any]:
    """Set the LED colour and switch to MANUAL mode.

    The colour is applied immediately and persisted to config.
    """
    from casectl.config.models import LedMode

    try:
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
