"""FastAPI routes for the fan-control plugin.

Mounted at ``/api/plugins/fan-control`` by the plugin host.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from casectl.plugins.fan.controller import FanController

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level reference to the controller — set by the plugin during setup.
# ---------------------------------------------------------------------------

_controller: FanController | None = None
_get_config: Any = None  # async callable returning config manager


def configure(controller: FanController, get_config: Any) -> None:
    """Wire the controller and config accessor into the route module.

    Called by :class:`FanControlPlugin` during ``setup()``.
    """
    global _controller, _get_config  # noqa: PLW0603
    _controller = controller
    _get_config = get_config


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class FanStatusResponse(BaseModel):
    """Response model for GET /status."""

    mode: str = Field(description="Current fan mode name")
    duty: list[int] = Field(description="Per-channel duty (0-255)")
    rpm: list[int] = Field(description="Per-channel RPM readings")
    temp: float = Field(description="Current CPU temperature in degrees C")
    degraded: bool = Field(description="Whether the controller is degraded")


class SetFanModeRequest(BaseModel):
    """Request body for POST /mode."""

    mode: int = Field(ge=0, le=4, description="Fan mode enum value (0-4)")


class SetFanSpeedRequest(BaseModel):
    """Request body for POST /speed."""

    duty: list[int] = Field(
        description="Per-channel duty in API range (0-100%)",
        min_length=1,
        max_length=3,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=FanStatusResponse)
async def fan_status() -> FanStatusResponse:
    """Return current fan mode, duty cycles, RPM readings, and temperature."""
    if _controller is None:
        raise HTTPException(status_code=503, detail="Fan controller not initialised")

    # Read RPM and temp from hardware if available.
    rpm: list[int] = [0, 0, 0]
    temp: float = 0.0

    if _controller._expansion is not None and _controller._expansion.connected:
        try:
            speeds = await _controller._expansion.async_get_motor_speed()
            rpm = list(speeds)
        except OSError:
            logger.debug("Failed to read motor speeds for status", exc_info=True)

    if _controller._system_info is not None:
        try:
            temp = _controller._system_info.get_cpu_temperature()
        except Exception:
            logger.debug("Failed to read CPU temp for status", exc_info=True)

    return FanStatusResponse(
        mode=_controller.current_mode.name.lower(),
        duty=_controller.current_duty,
        rpm=rpm,
        temp=temp,
        degraded=_controller.degraded,
    )


@router.post("/mode")
async def set_fan_mode(request: SetFanModeRequest) -> dict[str, str]:
    """Set the fan operating mode.

    Persists the new mode to config so it survives daemon restarts.
    """
    if _get_config is None:
        raise HTTPException(status_code=503, detail="Config manager not available")

    from casectl.config.models import FanMode

    try:
        FanMode(request.mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid fan mode: {request.mode}. Valid values: {[m.value for m in FanMode]}",
        )

    try:
        config_manager = _get_config()
        await config_manager.update("fan", {"mode": request.mode})
    except Exception as exc:
        logger.error("Failed to update fan mode config", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"status": "ok", "mode": FanMode(request.mode).name.lower()}


@router.post("/speed")
async def set_fan_speed(request: SetFanSpeedRequest) -> dict[str, Any]:
    """Set manual fan duty cycles.

    Accepts duty values in the 0-100 API range and converts to 0-255
    hardware range.  Also switches the fan mode to MANUAL.
    """
    if _get_config is None:
        raise HTTPException(status_code=503, detail="Config manager not available")

    from casectl.config.models import FanMode

    # Validate duty values are in 0-100 range.
    for d in request.duty:
        if not 0 <= d <= 100:
            raise HTTPException(
                status_code=400,
                detail=f"Duty values must be 0-100, got {d}",
            )

    # Convert 0-100% to 0-255 hardware range.
    hw_duty: list[int] = []
    for d in request.duty:
        hw_duty.append(int(d * 255 / 100))

    # Pad to 3 channels if fewer provided.
    while len(hw_duty) < 3:
        hw_duty.append(hw_duty[-1] if hw_duty else 0)

    try:
        config_manager = _get_config()
        await config_manager.update("fan", {
            "mode": FanMode.MANUAL.value,
            "manual_duty": hw_duty,
        })
    except Exception as exc:
        logger.error("Failed to update fan speed config", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"status": "ok", "duty_hw": hw_duty}
