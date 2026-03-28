"""MQTT state publishing and command subscription for casectl devices.

Provides bidirectional MQTT control for all casectl-managed devices (fans,
LEDs, OLED).  State is published to retained topics whenever device state
changes, and incoming MQTT commands are dispatched to the appropriate
config manager updates, achieving full parity with the REST API.

State topics (published)::

    casectl/fan/state        -> {"mode": "manual", "duty": [128,128,128]}
    casectl/fan/mode         -> "manual"
    casectl/fan/duty         -> "[128,128,128]"
    casectl/fan/curve        -> '[{"temp":30,"duty":20},{"temp":50,"duty":60}]'
    casectl/led/state        -> {"mode": "manual", "color": {"r":255,"g":0,"b":0}}
    casectl/led/mode         -> "rainbow"
    casectl/led/color        -> '{"r":255,"g":0,"b":0}'
    casectl/led/brightness   -> "255"
    casectl/oled/state       -> {"rotation": 0, "current_screen": 0}
    casectl/oled/rotation    -> "0"
    casectl/oled/screen      -> "0"

Command topics (subscribed)::

    casectl/fan/mode/set       <- "manual" | "follow-temp" | "off" | ...
    casectl/fan/duty/set       <- "[80,80,80]" (0-100 API range) or single int
    casectl/fan/duty/0/set     <- "80" (per-channel 0-100%, preserves others)
    casectl/fan/duty/1/set     <- "80"
    casectl/fan/duty/2/set     <- "80"
    casectl/fan/curve/set      <- '[{"temp":30,"duty":20},{"temp":50,"duty":60}]'
    casectl/led/mode/set       <- "rainbow" | "breathing" | "manual" | ...
    casectl/led/color/set      <- '{"r":255,"g":0,"b":0}' or "#FF0000"
    casectl/led/light/set      <- HA JSON light schema (state/color/brightness)
    casectl/led/brightness/set <- "128" (0-255 brightness, scales current color)
    casectl/oled/rotation/set  <- "0" | "180"
    casectl/oled/screen/set    <- "2" (screen index to switch to)

Each successful command also emits an ``mqtt_command_received`` event on the
event bus with details of the action, enabling the automation rules engine
and audit logging to react to MQTT-driven changes.

Home Assistant discovery entities are also published for controllable
devices (select entities for modes, number entities for fan duty, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from casectl.plugins.mqtt.ha_discovery import EntityDefinition

if TYPE_CHECKING:
    from casectl.config.manager import ConfigManager
    from casectl.daemon.event_bus import EventBus
    from casectl.plugins.mqtt.client import MqttConnectionManager

logger = logging.getLogger(__name__)

# Maximum number of points in a custom fan curve (constraint from spec)
MAX_CURVE_POINTS: int = 20


# ---------------------------------------------------------------------------
# HA discovery entities for controllable devices
# ---------------------------------------------------------------------------

COMMAND_ENTITIES: tuple[EntityDefinition, ...] = (
    # Fan mode select
    EntityDefinition(
        component="select",
        object_id="fan_mode",
        name="Fan Mode",
        state_topic_suffix="fan/mode",
        icon="mdi:fan-auto",
        extra={
            "command_topic": "",  # filled dynamically with prefix
            "options": ["follow-temp", "follow-rpi", "manual", "custom", "off"],
        },
    ),
    # Fan 1 duty (0-100%)
    EntityDefinition(
        component="number",
        object_id="fan_duty_0_set",
        name="Fan 1 Duty Set",
        state_topic_suffix="fan/duty",
        icon="mdi:fan-speed-1",
        unit_of_measurement="%",
        value_template="{{ value_json[0] }}",
        extra={
            "command_topic": "",  # filled dynamically
            "min": 0,
            "max": 100,
            "step": 1,
            "command_template": "{{ value }}",
        },
    ),
    # LED mode select
    EntityDefinition(
        component="select",
        object_id="led_mode",
        name="LED Mode",
        state_topic_suffix="led/mode",
        icon="mdi:led-on",
        extra={
            "command_topic": "",  # filled dynamically
            "options": [
                "rainbow",
                "breathing",
                "follow-temp",
                "manual",
                "custom",
                "off",
            ],
        },
    ),
    # LED colour as JSON RGB light
    EntityDefinition(
        component="light",
        object_id="led_light",
        name="LED Light",
        state_topic_suffix="led/state",
        icon="mdi:lightbulb",
        extra={
            "command_topic": "",  # filled dynamically
            "schema": "json",
            "brightness": True,
            "rgb": True,
            "color_mode": True,
            "supported_color_modes": ["rgb"],
            "value_template": "{{ value_json.mode }}",
            "state_value_template": "{% if value_json.mode != 'off' %}ON{% else %}OFF{% endif %}",
        },
    ),
    # Fan curve (JSON text entity)
    EntityDefinition(
        component="sensor",
        object_id="fan_curve",
        name="Fan Curve",
        state_topic_suffix="fan/curve",
        icon="mdi:chart-bell-curve-cumulative",
        extra={
            "value_template": "{{ value }}",
        },
    ),
    # OLED rotation select
    EntityDefinition(
        component="select",
        object_id="oled_rotation",
        name="OLED Rotation",
        state_topic_suffix="oled/rotation",
        icon="mdi:rotate-right",
        extra={
            "command_topic": "",  # filled dynamically
            "options": ["0", "180"],
        },
    ),
)


# ---------------------------------------------------------------------------
# Fan mode name ↔ integer mapping
# ---------------------------------------------------------------------------

_FAN_MODE_NAMES: dict[str, int] = {
    "follow-temp": 0,
    "follow_temp": 0,
    "follow-rpi": 1,
    "follow_rpi": 1,
    "manual": 2,
    "custom": 3,
    "off": 4,
}

_FAN_MODE_INTS: dict[int, str] = {
    0: "follow-temp",
    1: "follow-rpi",
    2: "manual",
    3: "custom",
    4: "off",
}

_LED_MODE_NAMES: dict[str, int] = {
    "rainbow": 0,
    "breathing": 1,
    "follow-temp": 2,
    "follow_temp": 2,
    "manual": 3,
    "custom": 4,
    "off": 5,
}

_LED_MODE_INTS: dict[int, str] = {
    0: "rainbow",
    1: "breathing",
    2: "follow-temp",
    3: "manual",
    4: "custom",
    5: "off",
}


# ---------------------------------------------------------------------------
# Hex colour parser
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _parse_color(payload: str) -> tuple[int, int, int] | None:
    """Parse a colour from JSON ``{"r":R,"g":G,"b":B}`` or hex ``#RRGGBB``.

    Returns (r, g, b) tuple or None if unparseable.
    """
    payload = payload.strip()

    # Try hex first
    m = _HEX_RE.match(payload)
    if m:
        hex_str = m.group(1)
        return (
            int(hex_str[0:2], 16),
            int(hex_str[2:4], 16),
            int(hex_str[4:6], 16),
        )

    # Try JSON object
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict):
            r = int(obj.get("r", obj.get("red", 0)))
            g = int(obj.get("g", obj.get("green", 0)))
            b = int(obj.get("b", obj.get("blue", 0)))
            return (
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
            )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return None


# ---------------------------------------------------------------------------
# Device state publisher / command subscriber
# ---------------------------------------------------------------------------


class DeviceStateManager:
    """Publishes device state to MQTT and handles incoming commands.

    Listens for ``fan_state_changed``, ``led_state_changed``, and
    ``oled_state_changed`` events on the event bus and publishes the
    corresponding state to MQTT retained topics.

    Also subscribes to ``{prefix}/{device}/{attr}/set`` command topics and
    translates incoming MQTT messages into config manager updates, achieving
    full bidirectional control.

    Parameters
    ----------
    mqtt_manager:
        A connected :class:`MqttConnectionManager`.
    config_manager:
        The daemon's :class:`ConfigManager` for applying commands.
    event_bus:
        The daemon's :class:`EventBus` for state change events.  If ``None``,
        state publishing must be triggered manually via :meth:`publish_fan_state`
        etc.
    topic_prefix:
        Override for the MQTT topic prefix (defaults to the manager's
        ``settings.topic_prefix``).
    """

    def __init__(
        self,
        mqtt_manager: MqttConnectionManager,
        config_manager: ConfigManager | None = None,
        event_bus: EventBus | None = None,
        *,
        topic_prefix: str | None = None,
    ) -> None:
        self._mqtt = mqtt_manager
        self._config_manager = config_manager
        self._event_bus = event_bus
        self._topic_prefix = topic_prefix or mqtt_manager.settings.topic_prefix
        self._subscribed: bool = False
        self._publish_count: int = 0
        self._command_count: int = 0
        self._error_count: int = 0
        self._command_topic_count: int = 0

        # Cache last-published state to avoid redundant publishes.
        self._last_fan_state: dict[str, Any] | None = None
        self._last_led_state: dict[str, Any] | None = None
        self._last_oled_state: dict[str, Any] | None = None

    # -- properties ---------------------------------------------------------

    @property
    def topic_prefix(self) -> str:
        """The MQTT topic prefix used for device topics."""
        return self._topic_prefix

    @property
    def publish_count(self) -> int:
        """Total number of state publishes."""
        return self._publish_count

    @property
    def command_count(self) -> int:
        """Total number of commands received and processed."""
        return self._command_count

    @property
    def error_count(self) -> int:
        """Total number of errors encountered."""
        return self._error_count

    @property
    def is_subscribed(self) -> bool:
        """Whether command topic subscriptions are active."""
        return self._subscribed

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to event bus events and MQTT command topics.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._subscribed:
            return

        # Subscribe to event bus for state changes
        if self._event_bus is not None:
            self._event_bus.subscribe("fan_state_changed", self._on_fan_state_changed)
            self._event_bus.subscribe("led_state_changed", self._on_led_state_changed)
            self._event_bus.subscribe("oled_state_changed", self._on_oled_state_changed)

        # Subscribe to MQTT command topics
        if self._mqtt.is_connected:
            await self._subscribe_command_topics()

        self._subscribed = True
        logger.info("DeviceStateManager started (prefix=%s)", self._topic_prefix)

    async def stop(self) -> None:
        """Unsubscribe from events and command topics."""
        if self._event_bus is not None:
            self._event_bus.unsubscribe("fan_state_changed", self._on_fan_state_changed)
            self._event_bus.unsubscribe("led_state_changed", self._on_led_state_changed)
            self._event_bus.unsubscribe("oled_state_changed", self._on_oled_state_changed)

        if self._mqtt.is_connected:
            await self._unsubscribe_command_topics()

        self._subscribed = False
        logger.info("DeviceStateManager stopped")

    # -- MQTT command topic subscription ------------------------------------

    async def _subscribe_command_topics(self) -> None:
        """Subscribe to all ``{prefix}/.../set`` command topics."""
        prefix = self._topic_prefix
        topics = [
            (f"{prefix}/fan/mode/set", self._handle_fan_mode_command),
            (f"{prefix}/fan/duty/set", self._handle_fan_duty_command),
            (f"{prefix}/fan/duty/0/set", self._make_channel_duty_handler(0)),
            (f"{prefix}/fan/duty/1/set", self._make_channel_duty_handler(1)),
            (f"{prefix}/fan/duty/2/set", self._make_channel_duty_handler(2)),
            (f"{prefix}/fan/curve/set", self._handle_fan_curve_command),
            (f"{prefix}/led/mode/set", self._handle_led_mode_command),
            (f"{prefix}/led/color/set", self._handle_led_color_command),
            (f"{prefix}/led/light/set", self._handle_led_light_command),
            (f"{prefix}/led/brightness/set", self._handle_led_brightness_command),
            (f"{prefix}/oled/rotation/set", self._handle_oled_rotation_command),
            (f"{prefix}/oled/screen/set", self._handle_oled_screen_command),
        ]
        self._command_topic_count = len(topics)
        for topic, handler in topics:
            await self._mqtt.subscribe(topic, handler)
        logger.debug("Subscribed to %d command topics", len(topics))

    async def _unsubscribe_command_topics(self) -> None:
        """Unsubscribe from all command topics."""
        prefix = self._topic_prefix
        topics = [
            f"{prefix}/fan/mode/set",
            f"{prefix}/fan/duty/set",
            f"{prefix}/fan/duty/0/set",
            f"{prefix}/fan/duty/1/set",
            f"{prefix}/fan/duty/2/set",
            f"{prefix}/fan/curve/set",
            f"{prefix}/led/mode/set",
            f"{prefix}/led/color/set",
            f"{prefix}/led/light/set",
            f"{prefix}/led/brightness/set",
            f"{prefix}/oled/rotation/set",
            f"{prefix}/oled/screen/set",
        ]
        for topic in topics:
            try:
                await self._mqtt.unsubscribe(topic)
            except Exception:
                logger.debug("Failed to unsubscribe from %s", topic)

    # -- state publishing ---------------------------------------------------

    async def publish_fan_state(
        self,
        mode: str,
        duty: list[int],
        *,
        force: bool = False,
    ) -> None:
        """Publish fan state to MQTT.

        Parameters
        ----------
        mode:
            Fan mode name (e.g. ``"manual"``, ``"follow-temp"``).
        duty:
            Per-channel duty values (0-255 hardware range).
        force:
            Publish even if state hasn't changed.
        """
        state = {"mode": mode, "duty": duty}
        if not force and state == self._last_fan_state:
            return

        if not self._mqtt.is_connected:
            return

        prefix = self._topic_prefix
        try:
            # Publish combined state
            await self._mqtt.publish(
                f"{prefix}/fan/state",
                json.dumps(state, separators=(",", ":")),
            )
            # Publish individual attributes
            await self._mqtt.publish(f"{prefix}/fan/mode", mode)
            # Convert 0-255 to 0-100 for API parity
            duty_pct = [min(100, round(d * 100 / 255)) for d in duty]
            await self._mqtt.publish(
                f"{prefix}/fan/duty",
                json.dumps(duty_pct, separators=(",", ":")),
            )
            self._last_fan_state = state
            self._publish_count += 1
            logger.debug("Published fan state: mode=%s duty=%s", mode, duty_pct)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to publish fan state: %s", exc)

    async def publish_led_state(
        self,
        mode: str,
        color: dict[str, int],
        *,
        force: bool = False,
    ) -> None:
        """Publish LED state to MQTT.

        Parameters
        ----------
        mode:
            LED mode name (e.g. ``"rainbow"``, ``"manual"``).
        color:
            RGB colour dict with ``"red"``, ``"green"``, ``"blue"`` keys.
        force:
            Publish even if state hasn't changed.
        """
        state = {"mode": mode, "color": color}
        if not force and state == self._last_led_state:
            return

        if not self._mqtt.is_connected:
            return

        prefix = self._topic_prefix
        try:
            await self._mqtt.publish(
                f"{prefix}/led/state",
                json.dumps(state, separators=(",", ":")),
            )
            await self._mqtt.publish(f"{prefix}/led/mode", mode)
            color_compact = {
                "r": color.get("red", 0),
                "g": color.get("green", 0),
                "b": color.get("blue", 0),
            }
            await self._mqtt.publish(
                f"{prefix}/led/color",
                json.dumps(color_compact, separators=(",", ":")),
            )
            # Compute approximate brightness (max channel)
            brightness = max(
                color.get("red", 0),
                color.get("green", 0),
                color.get("blue", 0),
            )
            await self._mqtt.publish(f"{prefix}/led/brightness", str(brightness))
            self._last_led_state = state
            self._publish_count += 1
            logger.debug("Published LED state: mode=%s color=%s", mode, color_compact)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to publish LED state: %s", exc)

    async def publish_oled_state(
        self,
        rotation: int,
        current_screen: int,
        *,
        force: bool = False,
    ) -> None:
        """Publish OLED state to MQTT.

        Parameters
        ----------
        rotation:
            Display rotation in degrees (0 or 180).
        current_screen:
            Index of the currently displayed screen.
        force:
            Publish even if state hasn't changed.
        """
        state = {"rotation": rotation, "current_screen": current_screen}
        if not force and state == self._last_oled_state:
            return

        if not self._mqtt.is_connected:
            return

        prefix = self._topic_prefix
        try:
            await self._mqtt.publish(
                f"{prefix}/oled/state",
                json.dumps(state, separators=(",", ":")),
            )
            await self._mqtt.publish(f"{prefix}/oled/rotation", str(rotation))
            await self._mqtt.publish(f"{prefix}/oled/screen", str(current_screen))
            self._last_oled_state = state
            self._publish_count += 1
            logger.debug(
                "Published OLED state: rotation=%d screen=%d",
                rotation,
                current_screen,
            )
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to publish OLED state: %s", exc)

    # -- event bus handlers -------------------------------------------------

    async def _on_fan_state_changed(self, data: dict[str, Any]) -> None:
        """Handle ``fan_state_changed`` event from the fan control plugin."""
        mode = data.get("mode", "unknown")
        duty = data.get("duty", [0, 0, 0])
        await self.publish_fan_state(mode, duty)

    async def _on_led_state_changed(self, data: dict[str, Any]) -> None:
        """Handle ``led_state_changed`` event from the LED control plugin."""
        mode = data.get("mode", "unknown")
        color = data.get("color", {"red": 0, "green": 0, "blue": 0})
        await self.publish_led_state(mode, color)

    async def _on_oled_state_changed(self, data: dict[str, Any]) -> None:
        """Handle ``oled_state_changed`` event from the OLED display plugin."""
        rotation = data.get("rotation", 0)
        current_screen = data.get("current_screen", 0)
        await self.publish_oled_state(rotation, current_screen)

    # -- command event emission ---------------------------------------------

    async def _emit_command_event(
        self,
        device: str,
        action: str,
        value: Any,
        *,
        topic: str = "",
    ) -> None:
        """Emit an ``mqtt_command_received`` event on the event bus.

        This enables the automation rules engine and audit logging to react
        to MQTT-driven control changes.

        Parameters
        ----------
        device:
            Device category (e.g. ``"fan"``, ``"led"``, ``"oled"``).
        action:
            The action performed (e.g. ``"mode"``, ``"duty"``, ``"color"``).
        value:
            The parsed value that was applied.
        topic:
            The originating MQTT topic (for audit/debug).
        """
        if self._event_bus is None:
            return

        try:
            await self._event_bus.emit(
                "mqtt_command_received",
                {
                    "device": device,
                    "action": action,
                    "value": value,
                    "topic": topic,
                    "source": "mqtt",
                },
            )
        except Exception:
            logger.debug("Failed to emit mqtt_command_received event")

    # -- MQTT command handlers ----------------------------------------------

    async def _handle_fan_mode_command(self, topic: str, payload: bytes) -> None:
        """Handle ``fan/mode/set`` MQTT command.

        Accepts mode name (e.g. ``"manual"``) or integer (e.g. ``"2"``).
        """
        if self._config_manager is None:
            logger.warning("Cannot handle fan mode command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip().lower()
        mode_int: int | None = None

        # Try name lookup
        if value in _FAN_MODE_NAMES:
            mode_int = _FAN_MODE_NAMES[value]
        else:
            # Try integer
            try:
                parsed = int(value)
                if parsed in _FAN_MODE_INTS:
                    mode_int = parsed
            except ValueError:
                pass

        if mode_int is None:
            logger.warning("Invalid fan mode command: %r", value)
            self._error_count += 1
            return

        try:
            await self._config_manager.update("fan", {"mode": mode_int})
            self._command_count += 1
            mode_name = _FAN_MODE_INTS[mode_int]
            logger.info("MQTT command: set fan mode to %s (%d)", mode_name, mode_int)
            await self._emit_command_event("fan", "mode", mode_name, topic=topic)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply fan mode command: %s", exc)

    async def _handle_fan_duty_command(self, topic: str, payload: bytes) -> None:
        """Handle ``fan/duty/set`` MQTT command.

        Accepts JSON array of 1-3 duty values in 0-100 API range,
        or a single integer value applied to all channels.
        """
        if self._config_manager is None:
            logger.warning("Cannot handle fan duty command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()
        duty_pct: list[int] = []

        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                duty_pct = [max(0, min(100, int(v))) for v in parsed]
            elif isinstance(parsed, int | float):
                duty_pct = [max(0, min(100, int(parsed)))]
            else:
                raise ValueError(f"Unexpected type: {type(parsed)}")
        except (json.JSONDecodeError, TypeError, ValueError):
            # Try single integer
            try:
                single = max(0, min(100, int(value)))
                duty_pct = [single]
            except ValueError:
                logger.warning("Invalid fan duty command: %r", value)
                self._error_count += 1
                return

        # Convert 0-100% to 0-255 hardware range
        hw_duty = [int(d * 255 / 100) for d in duty_pct]

        # Pad to 3 channels
        while len(hw_duty) < 3:
            hw_duty.append(hw_duty[-1] if hw_duty else 0)
        hw_duty = hw_duty[:3]

        try:
            from casectl.config.models import FanMode

            await self._config_manager.update(
                "fan",
                {"mode": FanMode.MANUAL.value, "manual_duty": hw_duty},
            )
            self._command_count += 1
            logger.info("MQTT command: set fan duty to %s (hw: %s)", duty_pct, hw_duty)
            await self._emit_command_event("fan", "duty", duty_pct, topic=topic)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply fan duty command: %s", exc)

    def _make_channel_duty_handler(
        self, channel: int
    ) -> Callable[[str, bytes], Coroutine[Any, Any, None]]:
        """Create a handler for per-channel fan duty set commands.

        Parameters
        ----------
        channel:
            Fan channel index (0, 1, or 2).

        Returns
        -------
        Callable
            An async handler for ``fan/duty/{channel}/set``.
        """

        async def handler(topic: str, payload: bytes) -> None:
            await self._handle_fan_channel_duty_command(topic, payload, channel)

        return handler

    async def _handle_fan_channel_duty_command(
        self, topic: str, payload: bytes, channel: int
    ) -> None:
        """Handle ``fan/duty/{channel}/set`` MQTT command.

        Sets a single fan channel duty (0-100 API range) while preserving
        the other channels at their current values.

        Parameters
        ----------
        topic:
            The MQTT topic (for logging).
        payload:
            Duty value as bytes (0-100 percent).
        channel:
            Fan channel index (0, 1, or 2).
        """
        if self._config_manager is None:
            logger.warning("Cannot handle fan channel duty command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()

        try:
            duty_pct = max(0, min(100, int(float(value))))
        except (ValueError, TypeError):
            logger.warning("Invalid fan channel %d duty command: %r", channel, value)
            self._error_count += 1
            return

        hw_duty = int(duty_pct * 255 / 100)

        try:
            from casectl.config.models import FanMode

            # Read current duty to preserve other channels
            raw = await self._config_manager.get("fan")
            current_duty = (
                list(raw.get("manual_duty", [75, 75, 75]))
                if isinstance(raw, dict)
                else [75, 75, 75]
            )
            # Pad to 3 channels if needed
            while len(current_duty) < 3:
                current_duty.append(75)
            current_duty = current_duty[:3]

            current_duty[channel] = hw_duty
            await self._config_manager.update(
                "fan",
                {"mode": FanMode.MANUAL.value, "manual_duty": current_duty},
            )
            self._command_count += 1
            logger.info(
                "MQTT command: set fan channel %d duty to %d%% (hw: %d)",
                channel,
                duty_pct,
                hw_duty,
            )
            await self._emit_command_event(
                "fan", f"duty_channel_{channel}", duty_pct, topic=topic
            )
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply fan channel duty command: %s", exc)

    async def _handle_fan_curve_command(self, topic: str, payload: bytes) -> None:
        """Handle ``fan/curve/set`` MQTT command.

        Accepts a JSON array of ``{"temp": T, "duty": D}`` objects defining
        a custom fan curve.  Each point maps a temperature (°C) to a duty
        cycle percentage (0-100).  The array must be sorted by temperature
        and contain between 2 and :data:`MAX_CURVE_POINTS` entries.

        Example payload::

            [{"temp": 30, "duty": 20}, {"temp": 50, "duty": 60}, {"temp": 70, "duty": 100}]

        On success, the fan mode is switched to CUSTOM and the curve is
        persisted to configuration.
        """
        if self._config_manager is None:
            logger.warning("Cannot handle fan curve command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()

        try:
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError(f"Expected JSON array, got {type(parsed).__name__}")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Invalid fan curve command (not JSON array): %r — %s", value, exc)
            self._error_count += 1
            return

        # Validate curve points
        if len(parsed) < 2:
            logger.warning("Fan curve must have at least 2 points, got %d", len(parsed))
            self._error_count += 1
            return

        if len(parsed) > MAX_CURVE_POINTS:
            logger.warning(
                "Fan curve exceeds maximum of %d points (got %d)",
                MAX_CURVE_POINTS,
                len(parsed),
            )
            self._error_count += 1
            return

        curve_points: list[dict[str, int]] = []
        prev_temp: int | None = None
        for i, point in enumerate(parsed):
            if not isinstance(point, dict):
                logger.warning("Fan curve point %d is not an object: %r", i, point)
                self._error_count += 1
                return

            try:
                temp = int(point.get("temp", point.get("temperature", 0)))
                duty = int(point.get("duty", point.get("duty_percent", 0)))
            except (TypeError, ValueError) as exc:
                logger.warning("Fan curve point %d has invalid values: %s", i, exc)
                self._error_count += 1
                return

            # Clamp duty to 0-100
            duty = max(0, min(100, duty))

            # Ensure monotonically increasing temperature
            if prev_temp is not None and temp <= prev_temp:
                logger.warning(
                    "Fan curve temperatures must be strictly increasing "
                    "(point %d: %d <= %d)",
                    i,
                    temp,
                    prev_temp,
                )
                self._error_count += 1
                return

            prev_temp = temp
            curve_points.append({"temp": temp, "duty": duty})

        try:
            from casectl.config.models import FanMode

            await self._config_manager.update(
                "fan",
                {"mode": FanMode.CUSTOM.value, "curve_points": curve_points},
            )
            self._command_count += 1
            logger.info(
                "MQTT command: set fan curve with %d points (%d°C–%d°C)",
                len(curve_points),
                curve_points[0]["temp"],
                curve_points[-1]["temp"],
            )
            await self._emit_command_event("fan", "curve", curve_points, topic=topic)

            # Publish the curve to MQTT for state feedback
            if self._mqtt.is_connected:
                await self._mqtt.publish(
                    f"{self._topic_prefix}/fan/curve",
                    json.dumps(curve_points, separators=(",", ":")),
                )
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply fan curve command: %s", exc)

    async def _handle_led_mode_command(self, topic: str, payload: bytes) -> None:
        """Handle ``led/mode/set`` MQTT command.

        Accepts mode name (e.g. ``"rainbow"``) or integer (e.g. ``"0"``).
        """
        if self._config_manager is None:
            logger.warning("Cannot handle LED mode command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip().lower()
        mode_int: int | None = None

        if value in _LED_MODE_NAMES:
            mode_int = _LED_MODE_NAMES[value]
        else:
            try:
                parsed = int(value)
                if parsed in _LED_MODE_INTS:
                    mode_int = parsed
            except ValueError:
                pass

        if mode_int is None:
            logger.warning("Invalid LED mode command: %r", value)
            self._error_count += 1
            return

        try:
            await self._config_manager.update("led", {"mode": mode_int})
            self._command_count += 1
            mode_name = _LED_MODE_INTS[mode_int]
            logger.info("MQTT command: set LED mode to %s (%d)", mode_name, mode_int)
            await self._emit_command_event("led", "mode", mode_name, topic=topic)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply LED mode command: %s", exc)

    async def _handle_led_color_command(self, topic: str, payload: bytes) -> None:
        """Handle ``led/color/set`` MQTT command.

        Accepts JSON ``{"r":R,"g":G,"b":B}`` or hex ``#RRGGBB``.
        """
        if self._config_manager is None:
            logger.warning("Cannot handle LED color command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()
        rgb = _parse_color(value)

        if rgb is None:
            logger.warning("Invalid LED color command: %r", value)
            self._error_count += 1
            return

        try:
            from casectl.config.models import LedMode

            await self._config_manager.update(
                "led",
                {
                    "mode": LedMode.MANUAL.value,
                    "red_value": rgb[0],
                    "green_value": rgb[1],
                    "blue_value": rgb[2],
                },
            )
            self._command_count += 1
            logger.info("MQTT command: set LED color to (%d, %d, %d)", *rgb)
            await self._emit_command_event(
                "led", "color", {"r": rgb[0], "g": rgb[1], "b": rgb[2]}, topic=topic
            )
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply LED color command: %s", exc)

    async def _handle_led_light_command(self, topic: str, payload: bytes) -> None:
        """Handle ``led/light/set`` MQTT command (HA JSON light schema).

        Accepts Home Assistant JSON light payloads such as::

            {"state": "ON", "color": {"r": 255, "g": 0, "b": 0}, "brightness": 128}
            {"state": "OFF"}
            {"brightness": 200}
            {"color": {"r": 0, "g": 255, "b": 0}}

        When ``state`` is ``"OFF"``, switches LED mode to OFF.
        When ``state`` is ``"ON"`` (or absent), switches to MANUAL mode and
        applies any provided color and/or brightness.
        """
        if self._config_manager is None:
            logger.warning("Cannot handle LED light command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()

        try:
            obj = json.loads(value)
            if not isinstance(obj, dict):
                raise ValueError(f"Expected JSON object, got {type(obj).__name__}")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Invalid LED light command (not JSON object): %r — %s", value, exc)
            self._error_count += 1
            return

        try:
            from casectl.config.models import LedMode

            state = obj.get("state", "ON").upper()

            if state == "OFF":
                await self._config_manager.update("led", {"mode": LedMode.OFF.value})
                self._command_count += 1
                logger.info("MQTT command: set LED light OFF")
                await self._emit_command_event("led", "light", {"state": "OFF"}, topic=topic)
                return

            # Build update dict — always switch to MANUAL when controlling
            update: dict[str, Any] = {"mode": LedMode.MANUAL.value}

            # Extract colour from HA color object {"r":R,"g":G,"b":B}
            color = obj.get("color")
            if isinstance(color, dict):
                update["red_value"] = max(0, min(255, int(color.get("r", 0))))
                update["green_value"] = max(0, min(255, int(color.get("g", 0))))
                update["blue_value"] = max(0, min(255, int(color.get("b", 0))))

            # HA brightness is 0-255, scale RGB channels proportionally
            brightness = obj.get("brightness")
            if brightness is not None:
                brightness = max(0, min(255, int(brightness)))
                # If we have colour values from this command, scale them
                if color and isinstance(color, dict):
                    r = update.get("red_value", 0)
                    g = update.get("green_value", 0)
                    b = update.get("blue_value", 0)
                    max_ch = max(r, g, b) or 1
                    scale = brightness / max_ch
                    update["red_value"] = max(0, min(255, round(r * scale)))
                    update["green_value"] = max(0, min(255, round(g * scale)))
                    update["blue_value"] = max(0, min(255, round(b * scale)))
                elif not color:
                    # No colour in this command — read current and scale
                    raw = await self._config_manager.get("led")
                    if isinstance(raw, dict):
                        r = raw.get("red_value", 0)
                        g = raw.get("green_value", 0)
                        b = raw.get("blue_value", 255)
                    else:
                        r, g, b = 0, 0, 255
                    max_ch = max(r, g, b) or 1
                    scale = brightness / max_ch
                    update["red_value"] = max(0, min(255, round(r * scale)))
                    update["green_value"] = max(0, min(255, round(g * scale)))
                    update["blue_value"] = max(0, min(255, round(b * scale)))

            await self._config_manager.update("led", update)
            self._command_count += 1
            logger.info("MQTT command: set LED light %s", update)
            await self._emit_command_event("led", "light", update, topic=topic)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply LED light command: %s", exc)

    async def _handle_led_brightness_command(self, topic: str, payload: bytes) -> None:
        """Handle ``led/brightness/set`` MQTT command.

        Accepts a brightness value (0-255).  Scales the current LED colour
        proportionally so the maximum channel equals the requested brightness.
        A brightness of 0 switches the LED mode to OFF.
        """
        if self._config_manager is None:
            logger.warning("Cannot handle LED brightness command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()

        try:
            brightness = max(0, min(255, int(float(value))))
        except (ValueError, TypeError):
            logger.warning("Invalid LED brightness command: %r", value)
            self._error_count += 1
            return

        try:
            from casectl.config.models import LedMode

            if brightness == 0:
                await self._config_manager.update("led", {"mode": LedMode.OFF.value})
                self._command_count += 1
                logger.info("MQTT command: set LED brightness to 0 (OFF)")
                await self._emit_command_event("led", "brightness", 0, topic=topic)
                return

            # Read current colour and scale
            raw = await self._config_manager.get("led")
            if isinstance(raw, dict):
                r = raw.get("red_value", 0)
                g = raw.get("green_value", 0)
                b = raw.get("blue_value", 255)
            else:
                r, g, b = 0, 0, 255

            max_ch = max(r, g, b) or 1
            scale = brightness / max_ch
            await self._config_manager.update(
                "led",
                {
                    "mode": LedMode.MANUAL.value,
                    "red_value": max(0, min(255, round(r * scale))),
                    "green_value": max(0, min(255, round(g * scale))),
                    "blue_value": max(0, min(255, round(b * scale))),
                },
            )
            self._command_count += 1
            logger.info("MQTT command: set LED brightness to %d", brightness)
            await self._emit_command_event("led", "brightness", brightness, topic=topic)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply LED brightness command: %s", exc)

    async def _handle_oled_rotation_command(self, topic: str, payload: bytes) -> None:
        """Handle ``oled/rotation/set`` MQTT command.

        Accepts ``"0"`` or ``"180"``.
        """
        if self._config_manager is None:
            logger.warning("Cannot handle OLED rotation command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()

        try:
            rotation = int(value)
        except ValueError:
            logger.warning("Invalid OLED rotation command: %r", value)
            self._error_count += 1
            return

        if rotation not in (0, 180):
            logger.warning("Invalid OLED rotation value: %d (must be 0 or 180)", rotation)
            self._error_count += 1
            return

        try:
            await self._config_manager.update("oled", {"rotation": rotation})
            self._command_count += 1
            logger.info("MQTT command: set OLED rotation to %d", rotation)
            await self._emit_command_event("oled", "rotation", rotation, topic=topic)
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply OLED rotation command: %s", exc)

    async def _handle_oled_screen_command(self, topic: str, payload: bytes) -> None:
        """Handle ``oled/screen/set`` MQTT command.

        Accepts an integer screen index (0-3).
        """
        if self._config_manager is None:
            logger.warning("Cannot handle OLED screen command — no config manager")
            return

        value = payload.decode("utf-8", errors="replace").strip()

        try:
            screen_index = int(value)
        except ValueError:
            logger.warning("Invalid OLED screen command: %r", value)
            self._error_count += 1
            return

        if screen_index < 0 or screen_index > 3:
            logger.warning(
                "OLED screen index out of range: %d (must be 0-3)", screen_index
            )
            self._error_count += 1
            return

        try:
            # Read existing screens config, enable the target screen
            raw = await self._config_manager.get("oled")
            screens = raw.get("screens", []) if isinstance(raw, dict) else []
            if screen_index < len(screens):
                screens[screen_index]["enabled"] = True
                await self._config_manager.update("oled", {"screens": screens})
                self._command_count += 1
                logger.info("MQTT command: set OLED screen to %d", screen_index)
                await self._emit_command_event(
                    "oled", "screen", screen_index, topic=topic
                )
            else:
                logger.warning(
                    "OLED screen index %d out of range (max %d)",
                    screen_index,
                    len(screens) - 1,
                )
                self._error_count += 1
        except Exception as exc:
            self._error_count += 1
            logger.warning("Failed to apply OLED screen command: %s", exc)

    # -- HA discovery helpers -----------------------------------------------

    def get_command_entities(self) -> tuple[EntityDefinition, ...]:
        """Return HA discovery entities with command_topic filled in.

        Returns
        -------
        tuple of EntityDefinition
            Entities ready for :class:`HADiscoveryManager`.
        """
        prefix = self._topic_prefix
        command_topic_map = {
            "fan_mode": f"{prefix}/fan/mode/set",
            "fan_duty_0_set": f"{prefix}/fan/duty/0/set",
            "led_mode": f"{prefix}/led/mode/set",
            "led_light": f"{prefix}/led/light/set",
            "oled_rotation": f"{prefix}/oled/rotation/set",
        }

        result: list[EntityDefinition] = []
        for entity in COMMAND_ENTITIES:
            if entity.object_id in command_topic_map:
                new_extra = dict(entity.extra)
                new_extra["command_topic"] = command_topic_map[entity.object_id]
                # EntityDefinition is frozen, so reconstruct
                result.append(
                    EntityDefinition(
                        component=entity.component,
                        object_id=entity.object_id,
                        name=entity.name,
                        state_topic_suffix=entity.state_topic_suffix,
                        icon=entity.icon,
                        device_class=entity.device_class,
                        state_class=entity.state_class,
                        unit_of_measurement=entity.unit_of_measurement,
                        value_template=entity.value_template,
                        payload_on=entity.payload_on,
                        payload_off=entity.payload_off,
                        enabled_by_default=entity.enabled_by_default,
                        entity_category=entity.entity_category,
                        extra=new_extra,
                    )
                )
            else:
                result.append(entity)

        return tuple(result)

    # -- status -------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return diagnostic information about the device state manager.

        Returns
        -------
        dict
            Keys: ``subscribed``, ``publish_count``, ``command_count``,
            ``error_count``, ``topic_prefix``.
        """
        return {
            "subscribed": self._subscribed,
            "publish_count": self._publish_count,
            "command_count": self._command_count,
            "error_count": self._error_count,
            "topic_prefix": self._topic_prefix,
        }
