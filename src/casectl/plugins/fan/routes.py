"""FastAPI routes for the fan-control plugin.

Mounted at ``/api/plugins/fan-control`` by the plugin host.

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
    from casectl.plugins.fan.controller import FanController

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency injection helpers
# ---------------------------------------------------------------------------


def _get_fan_controller(request: Request) -> FanController:
    """Retrieve the fan controller from ``app.state``.

    Raises :class:`HTTPException` 503 if the controller has not been set.
    """
    controller: FanController | None = getattr(request.app.state, "fan_controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Fan controller not initialised")
    return controller


def _get_fan_config_manager(request: Request) -> ConfigManager:
    """Retrieve the config manager from ``app.state``.

    Raises :class:`HTTPException` 503 if the config manager has not been set.
    """
    config_manager: ConfigManager | None = getattr(request.app.state, "fan_config_manager", None)
    if config_manager is None:
        raise HTTPException(status_code=503, detail="Config manager not available")
    return config_manager


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
    """Request body for PUT /mode."""

    mode: int | str = Field(description="Fan mode (0-4 or name: follow-temp, follow-rpi, manual, custom, off)")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        names = {"follow-temp": 0, "follow_temp": 0, "follow-rpi": 1, "follow_rpi": 1, "manual": 2, "custom": 3, "off": 4}
        if isinstance(v, str):
            if v.lower() in names:
                return names[v.lower()]
            raise ValueError(f"Unknown mode: {v}. Valid: {', '.join(names)}")
        if isinstance(v, int) and v not in range(5):
            raise ValueError(f"Mode must be 0-4, got {v}")
        return v


class SetFanSpeedRequest(BaseModel):
    """Request body for PUT /speed."""

    duty: list[Annotated[int, Field(ge=0, le=100)]] = Field(
        description="Per-channel duty in API range (0-100%)",
        min_length=1,
        max_length=3,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=FanStatusResponse)
async def fan_status(
    controller: Annotated[Any, Depends(_get_fan_controller)],
) -> FanStatusResponse:
    """Return current fan mode, duty cycles, RPM readings, and temperature."""
    # Read RPM and temp via public controller methods.
    rpm: list[int] = await controller.get_motor_speeds()
    temp: float = await controller.get_cpu_temperature()

    return FanStatusResponse(
        mode=controller.current_mode.name.lower(),
        duty=controller.current_duty,
        rpm=rpm,
        temp=temp,
        degraded=controller.degraded,
    )


@router.put("/mode")
async def set_fan_mode(
    request: SetFanModeRequest,
    config_manager: Annotated[Any, Depends(_get_fan_config_manager)],
) -> dict[str, str]:
    """Set the fan operating mode.

    Persists the new mode to config so it survives daemon restarts.
    """
    from casectl.config.models import FanMode

    try:
        FanMode(request.mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid fan mode: {request.mode}. Valid values: {[m.value for m in FanMode]}",
        )

    try:
        await config_manager.update("fan", {"mode": request.mode})
    except Exception as exc:
        logger.error("Failed to update fan mode config", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"status": "ok", "mode": FanMode(request.mode).name.lower()}


@router.put("/speed")
async def set_fan_speed(
    request: SetFanSpeedRequest,
    config_manager: Annotated[Any, Depends(_get_fan_config_manager)],
) -> dict[str, Any]:
    """Set manual fan duty cycles.

    Accepts duty values in the 0-100 API range and converts to 0-255
    hardware range.  Also switches the fan mode to MANUAL.
    """
    from casectl.config.models import FanMode

    # Convert 0-100% to 0-255 hardware range.
    hw_duty: list[int] = []
    for d in request.duty:
        hw_duty.append(int(d * 255 / 100))

    # Pad to 3 channels if fewer provided.
    while len(hw_duty) < 3:
        hw_duty.append(hw_duty[-1] if hw_duty else 0)

    try:
        await config_manager.update("fan", {
            "mode": FanMode.MANUAL.value,
            "manual_duty": hw_duty,
        })
    except Exception as exc:
        logger.error("Failed to update fan speed config", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"status": "ok", "duty_hw": hw_duty}
