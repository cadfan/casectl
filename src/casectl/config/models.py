"""Pydantic v2 configuration schemas and runtime models for casectl.

Defines all configuration models (persisted to config.yaml) and runtime
models (ephemeral, never saved) used throughout the application.
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LedMode(IntEnum):
    """LED operating modes.

    RAINBOW     - Cycle through rainbow colours automatically.
    BREATHING   - Pulsing brightness effect on a single colour.
    FOLLOW_TEMP - Colour shifts from cool→hot based on CPU temperature.
    MANUAL      - Fixed colour set via red/green/blue values.
    CUSTOM      - Reserved for plugin-defined patterns.
    OFF         - LEDs disabled.
    """

    RAINBOW = 0
    BREATHING = 1
    FOLLOW_TEMP = 2
    MANUAL = 3
    CUSTOM = 4
    OFF = 5


class FanMode(IntEnum):
    """Fan operating modes.

    FOLLOW_TEMP - Duty cycle tracks case/CPU temperature thresholds.
    FOLLOW_RPI  - Mirror the Raspberry Pi's own PWM fan signal.
    MANUAL      - Fixed duty cycle set via manual_duty values.
    CUSTOM      - Reserved for plugin-defined curves.
    OFF         - Fans disabled (duty = 0).
    """

    FOLLOW_TEMP = 0
    FOLLOW_RPI = 1
    MANUAL = 2
    CUSTOM = 3
    OFF = 4


# ---------------------------------------------------------------------------
# Fan configuration
# ---------------------------------------------------------------------------


class FanThresholds(BaseModel):
    """Temperature thresholds and corresponding fan speeds for FOLLOW_TEMP mode.

    Temperatures are in degrees Celsius.  Speed values are 0-255 PWM duty
    cycle values sent to the STM32 expansion board.

    The *schmitt* value provides hysteresis so the fan doesn't rapidly toggle
    between speed bands at a boundary temperature.
    """

    low_temp: int = Field(default=30, description="Below this temp → low_speed")
    high_temp: int = Field(default=50, description="Above this temp → high_speed")
    schmitt: int = Field(default=3, description="Hysteresis band in °C")
    low_speed: int = Field(default=75, description="PWM duty (0-255) for low temp range")
    mid_speed: int = Field(default=125, description="PWM duty (0-255) for mid temp range")
    high_speed: int = Field(default=175, description="PWM duty (0-255) for high temp range")


class FanConfig(BaseModel):
    """Configuration for the three case fans driven by the STM32 expansion board."""

    mode: FanMode = Field(default=FanMode.FOLLOW_TEMP, description="Fan operating mode")
    manual_duty: list[int] = Field(
        default=[75, 75, 75],
        description="Per-fan PWM duty (0-255) when mode is MANUAL",
    )

    @field_validator("manual_duty")
    @classmethod
    def _validate_manual_duty(cls, v: list[int]) -> list[int]:
        for i, val in enumerate(v):
            if not (0 <= val <= 255):
                msg = f"manual_duty[{i}] must be between 0 and 255, got {val}"
                raise ValueError(msg)
        return v

    thresholds: FanThresholds = Field(
        default_factory=FanThresholds,
        description="Temperature-based speed thresholds",
    )
    rpi_follow_min: int = Field(
        default=0,
        description="Minimum duty when following RPi fan (0-255)",
    )
    rpi_follow_max: int = Field(
        default=255,
        description="Maximum duty when following RPi fan (0-255)",
    )
    is_run_on_startup: bool = Field(
        default=True,
        description="Start fan control automatically when daemon launches",
    )


# ---------------------------------------------------------------------------
# LED configuration
# ---------------------------------------------------------------------------


class LedConfig(BaseModel):
    """Configuration for the RGB LEDs on the expansion board."""

    mode: LedMode = Field(default=LedMode.RAINBOW, description="LED operating mode")
    red_value: int = Field(default=0, ge=0, le=255, description="Red channel (0-255) for MANUAL mode")
    green_value: int = Field(default=0, ge=0, le=255, description="Green channel (0-255) for MANUAL mode")
    blue_value: int = Field(default=255, ge=0, le=255, description="Blue channel (0-255) for MANUAL mode")
    is_run_on_startup: bool = Field(
        default=True,
        description="Start LED control automatically when daemon launches",
    )


# ---------------------------------------------------------------------------
# OLED configuration
# ---------------------------------------------------------------------------


class OledScreenConfig(BaseModel):
    """Configuration for a single OLED display screen in the rotation cycle.

    Attributes:
        enabled: Whether this screen slot is shown in the rotation.
        display_time: Seconds to show this screen before advancing.
        date_format: Date format variant (0 = default).
        time_format: Time format variant (0 = 24h, 1 = 12h).
        interchange: Layout variant selector for the screen.
    """

    enabled: bool = Field(default=True, description="Show this screen in rotation")
    display_time: float = Field(default=5.0, description="Seconds to display before advancing")
    date_format: int = Field(default=0, description="Date format variant")
    time_format: int = Field(default=0, description="Time format variant (0=24h, 1=12h)")
    interchange: int = Field(default=0, description="Layout variant selector")


class OledConfig(BaseModel):
    """Configuration for the SSD1306 OLED display."""

    screens: list[OledScreenConfig] = Field(
        default_factory=lambda: [OledScreenConfig() for _ in range(4)],
        description="Screen slots in the display rotation cycle",
    )
    rotation: int = Field(default=180, description="Display rotation in degrees (0 or 180)")
    is_run_on_startup: bool = Field(
        default=True,
        description="Start OLED display automatically when daemon launches",
    )


# ---------------------------------------------------------------------------
# Service / API configuration
# ---------------------------------------------------------------------------


class ServiceConfig(BaseModel):
    """Configuration for the built-in FastAPI service."""

    api_port: int = Field(default=8420, description="Port for the REST API / web dashboard")
    api_host: str = Field(
        default="127.0.0.1",
        description="Bind address (use 0.0.0.0 for LAN access)",
    )
    trust_proxy: bool = Field(default=False, description="When True, check X-Forwarded-For instead of client IP for auth bypass. Set to True when behind a reverse proxy.")
    is_run_on_startup: bool = Field(
        default=False,
        description="Start the web API automatically when daemon launches",
    )


# ---------------------------------------------------------------------------
# Alert configuration
# ---------------------------------------------------------------------------


class AlertConfig(BaseModel):
    """Configuration for system alerts (webhook and/or email)."""

    enabled: bool = Field(default=False, description="Master enable for alerting")
    temp_threshold: float = Field(
        default=80.0,
        description="CPU temperature (°C) above which an alert fires",
    )
    disk_threshold: float = Field(
        default=90.0,
        description="Disk usage percent above which an alert fires",
    )
    webhook_url: str = Field(default="", description="HTTP(S) webhook URL for alerts")
    smtp_host: str = Field(default="", description="SMTP server hostname")
    smtp_port: int = Field(default=587, description="SMTP server port")
    smtp_user: str = Field(default="", description="SMTP username / sender address")
    smtp_password: str = Field(default="", description="SMTP password (stored in plaintext!)")
    smtp_to: str = Field(default="", description="Recipient email address")


# ---------------------------------------------------------------------------
# Top-level configuration
# ---------------------------------------------------------------------------


class CaseCtlConfig(BaseModel):
    """Root configuration model — serialised to ``config.yaml``.

    Each section corresponds to a top-level YAML key.  The *plugins* dict
    allows arbitrary per-plugin configuration without modifying this schema.
    """

    fan: FanConfig = Field(default_factory=FanConfig)
    led: LedConfig = Field(default_factory=LedConfig)
    oled: OledConfig = Field(default_factory=OledConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    plugins: dict[str, dict] = Field(
        default_factory=dict,
        description="Per-plugin configuration (plugin-name → settings dict)",
    )


# ---------------------------------------------------------------------------
# Runtime model (not persisted)
# ---------------------------------------------------------------------------


class SystemMetrics(BaseModel):
    """Live system metrics collected by the monitor plugin.

    This model is **never** written to config.yaml — it exists only in
    memory and is served via the REST API / event bus.
    """

    cpu_percent: float = Field(default=0.0, description="CPU utilisation (0-100)")
    memory_percent: float = Field(default=0.0, description="RAM utilisation (0-100)")
    disk_percent: float = Field(default=0.0, description="Root disk utilisation (0-100)")
    cpu_temp: float = Field(default=0.0, description="CPU die temperature (°C)")
    case_temp: float = Field(default=0.0, description="Case / ambient temperature (°C)")
    ip_address: str = Field(default="", description="Primary IP address")
    fan_duty: list[int] = Field(
        default=[0, 0, 0],
        description="Current PWM duty per fan (0-255)",
    )
    motor_speed: list[int] = Field(
        default=[0, 0, 0],
        description="Measured motor speed per fan (RPM or raw tach)",
    )
    date: str = Field(
        default="",
        description="Current date string for OLED display",
    )
    weekday: str = Field(
        default="",
        description="Current weekday name for OLED display",
    )
    time: str = Field(
        default="",
        description="Current time string for OLED display",
    )
    rpi_fan_duty: int = Field(
        default=0,
        description="Raspberry Pi's own fan PWM duty (0-255)",
    )
