"""Pydantic v2 configuration schemas and runtime models for casectl.

Defines all configuration models (persisted to config.yaml) and runtime
models (ephemeral, never saved) used throughout the application.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

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
    low_speed: int = Field(default=75, ge=0, le=255, description="PWM duty (0-255) for low temp range")
    mid_speed: int = Field(default=125, ge=0, le=255, description="PWM duty (0-255) for mid temp range")
    high_speed: int = Field(default=175, ge=0, le=255, description="PWM duty (0-255) for high temp range")


class FanCurvePoint(BaseModel):
    """A single point on a custom fan curve mapping temperature to PWM duty.

    Attributes:
        temp: Temperature in degrees Celsius at this curve point.
        duty: PWM duty cycle (0-255) at this temperature.
    """

    temp: float = Field(description="Temperature in °C")
    duty: int = Field(ge=0, le=255, description="PWM duty cycle (0-255)")


class FanCurveConfig(BaseModel):
    """Configuration for a custom multi-point fan curve.

    Defines a piecewise-linear mapping from temperature to PWM duty cycle.
    Points are sorted by temperature at validation time.  Between points,
    duty is linearly interpolated.  Below the lowest point, the lowest
    point's duty is used; above the highest, the highest point's duty is used.

    Attributes:
        name: Human-readable name for this curve profile.
        points: Ordered list of (temp, duty) curve points (2-20 points).
        hysteresis: Temperature hysteresis in °C to prevent oscillation.
    """

    name: str = Field(default="default", description="Curve profile name")
    points: list[FanCurvePoint] = Field(
        default_factory=lambda: [
            FanCurvePoint(temp=30.0, duty=75),
            FanCurvePoint(temp=50.0, duty=175),
        ],
        description="Curve points mapping temp→duty (2-20 points, sorted by temp)",
    )
    hysteresis: float = Field(
        default=2.0,
        ge=0.0,
        le=10.0,
        description="Temperature hysteresis in °C to prevent oscillation",
    )

    @field_validator("points")
    @classmethod
    def _validate_points(cls, v: list[FanCurvePoint]) -> list[FanCurvePoint]:
        if len(v) < 2:
            msg = f"Fan curve requires at least 2 points, got {len(v)}"
            raise ValueError(msg)
        if len(v) > 20:
            msg = f"Fan curve allows at most 20 points, got {len(v)}"
            raise ValueError(msg)
        # Sort by temperature
        v = sorted(v, key=lambda p: p.temp)
        # Check for duplicate temperatures
        temps = [p.temp for p in v]
        for i in range(1, len(temps)):
            if temps[i] == temps[i - 1]:
                msg = f"Duplicate temperature {temps[i]}°C in fan curve"
                raise ValueError(msg)
        return v


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
    curve: FanCurveConfig = Field(
        default_factory=FanCurveConfig,
        description="Custom multi-point fan curve for CUSTOM mode",
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
    """Configuration for system alerts (webhook, ntfy.sh, and/or email).

    Supports three alerting channels that can be used independently or together:

    * **Webhook** — POST JSON payloads to any HTTP(S) endpoint.
    * **ntfy.sh** — Push notifications via `ntfy.sh <https://ntfy.sh>`_ (self-hosted or cloud).
    * **SMTP** — Send email alerts via an SMTP server.

    Each channel is enabled implicitly when its required fields are non-empty.
    The ``enabled`` flag is a master switch that gates all channels.
    """

    enabled: bool = Field(default=False, description="Master enable for alerting")
    temp_threshold: float = Field(
        default=80.0,
        description="CPU temperature (°C) above which an alert fires",
    )
    disk_threshold: float = Field(
        default=90.0,
        description="Disk usage percent above which an alert fires",
    )
    cooldown: float = Field(
        default=300.0,
        ge=0.0,
        le=86400.0,
        description="Minimum seconds between repeated alerts of the same type",
    )

    # -- Webhook channel ---
    webhook_url: str = Field(default="", description="HTTP(S) webhook URL for alerts")
    webhook_method: str = Field(
        default="POST",
        description="HTTP method for webhook (POST or PUT)",
    )
    webhook_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra HTTP headers for webhook requests",
    )

    # -- ntfy.sh channel ---
    ntfy_url: str = Field(
        default="https://ntfy.sh",
        description="ntfy.sh server URL (cloud or self-hosted)",
    )
    ntfy_topic: str = Field(default="", description="ntfy.sh topic name")
    ntfy_token: str = Field(
        default="",
        description="ntfy.sh access token (optional, for private topics)",
    )
    ntfy_priority: int = Field(
        default=3,
        ge=1,
        le=5,
        description="ntfy.sh priority (1=min, 3=default, 5=max)",
    )

    # -- SMTP channel ---
    smtp_host: str = Field(default="", description="SMTP server hostname")
    smtp_port: int = Field(default=587, description="SMTP server port")
    smtp_user: str = Field(default="", description="SMTP username / sender address")
    smtp_password: str = Field(default="", description="SMTP password (stored in plaintext!)")
    smtp_to: str = Field(default="", description="Recipient email address")
    smtp_subject_prefix: str = Field(
        default="[casectl]",
        description="Subject line prefix for email alerts",
    )


# ---------------------------------------------------------------------------
# MQTT configuration
# ---------------------------------------------------------------------------


class MqttConfig(BaseModel):
    """Configuration for the MQTT integration plugin (BYO broker).

    Supports bidirectional MQTT communication with configurable broker
    settings, QoS 1 defaults, reconnection logic, and retained messages.
    Designed for Home Assistant auto-discovery via the ``homeassistant/``
    topic prefix.
    """

    enabled: bool = Field(default=False, description="Master enable for MQTT integration")
    broker_host: str = Field(
        default="localhost",
        description="MQTT broker hostname or IP address",
    )
    broker_port: int = Field(
        default=1883,
        ge=1,
        le=65535,
        description="MQTT broker port (1883 plain, 8883 TLS)",
    )
    username: str = Field(default="", description="MQTT broker username (empty for anonymous)")
    password: str = Field(default="", description="MQTT broker password")
    client_id: str = Field(
        default="casectl",
        description="MQTT client identifier (must be unique per broker)",
    )
    topic_prefix: str = Field(
        default="casectl",
        description="Base topic prefix for all published messages",
    )
    ha_discovery_prefix: str = Field(
        default="homeassistant",
        description="Home Assistant MQTT discovery topic prefix",
    )
    qos: int = Field(
        default=1,
        ge=0,
        le=2,
        description="Default MQTT QoS level (0=at most once, 1=at least once, 2=exactly once)",
    )
    retain: bool = Field(
        default=True,
        description="Retain published state messages on the broker",
    )
    keepalive: int = Field(
        default=60,
        ge=5,
        le=3600,
        description="MQTT keepalive interval in seconds",
    )
    reconnect_min_delay: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
        description="Minimum delay (seconds) before first reconnection attempt",
    )
    reconnect_max_delay: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="Maximum delay (seconds) between reconnection attempts (exponential backoff cap)",
    )
    tls_enabled: bool = Field(
        default=False,
        description="Enable TLS for MQTT broker connection",
    )
    tls_ca_cert: str = Field(
        default="",
        description="Path to CA certificate file for TLS verification",
    )
    tls_insecure: bool = Field(
        default=False,
        description="Skip TLS certificate verification (not recommended)",
    )
    birth_topic: str = Field(
        default="",
        description="Birth message topic (default: {topic_prefix}/status)",
    )
    will_topic: str = Field(
        default="",
        description="Last Will and Testament topic (default: {topic_prefix}/status)",
    )
    publish_interval: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description="Interval in seconds between metric publishing cycles",
    )


# ---------------------------------------------------------------------------
# Top-level configuration
# ---------------------------------------------------------------------------


class AutomationRuleConditionConfig(BaseModel):
    """A single condition for an automation rule."""

    field: str = Field(description="Dot-path into event data (e.g. 'cpu_temp')")
    operator: str = Field(description="Comparison: gt, gte, lt, lte, eq, neq, in, not_in, between")
    value: int | float | str | bool | list = Field(description="Reference value for comparison")


class AutomationRuleActionConfig(BaseModel):
    """An action to execute when an automation rule fires."""

    target: str = Field(description="Subsystem target (fan, led, oled, emit, log)")
    command: str = Field(description="Action command (e.g. 'set_duty', 'set_mode')")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the command",
    )


class AutomationRuleConfig(BaseModel):
    """A single automation rule in config.yaml."""

    name: str = Field(description="Unique human-readable rule name")
    description: str = Field(default="", description="Optional description")
    enabled: bool = Field(default=True, description="Whether this rule is active")
    priority: str = Field(
        default="user",
        description="Priority class: safety > scheduled > user",
    )
    event: str = Field(description="EventBus event to listen for")
    conditions: list[AutomationRuleConditionConfig] = Field(
        default_factory=list,
        description="All conditions must be true (AND logic)",
    )
    actions: list[AutomationRuleActionConfig] = Field(
        description="Actions to execute when conditions are met",
    )
    cooldown: float = Field(
        default=0.0,
        ge=0.0,
        le=3600.0,
        description="Minimum seconds between consecutive firings",
    )


class AutomationConfig(BaseModel):
    """Configuration for the event-driven automation rules engine."""

    enabled: bool = Field(default=False, description="Master enable for automation engine")
    rules: list[AutomationRuleConfig] = Field(
        default_factory=list,
        description="List of automation rules (max 100)",
    )

    @field_validator("rules")
    @classmethod
    def _validate_rules_limit(cls, v: list[AutomationRuleConfig]) -> list[AutomationRuleConfig]:
        if len(v) > 100:
            msg = f"Maximum 100 automation rules allowed, got {len(v)}"
            raise ValueError(msg)
        return v


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
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
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
