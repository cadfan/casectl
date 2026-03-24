"""Fan control logic for the casectl fan-control plugin.

Computes fan duty cycles based on the configured mode (follow_temp,
follow_rpi, manual, off) and applies them to the STM32 expansion board.
The STM32 is always set to FanHwMode.MANUAL — casectl controls duty directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from casectl.config.models import FanConfig, FanMode
from casectl.hardware.expansion import FanHwMode

if TYPE_CHECKING:
    from casectl.config.manager import ConfigManager
    from casectl.hardware.expansion import ExpansionBoard
    from casectl.hardware.system import SystemInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL: float = 3.0  # seconds between control loop iterations


class FanController:
    """Computes and applies fan duty cycles based on configuration and metrics.

    The controller supports four modes:

    - **follow_temp**: Linear ramp between low_temp and high_temp thresholds
      with Schmitt trigger hysteresis to prevent oscillation at boundaries.
    - **follow_rpi**: Reads the Raspberry Pi's sysfs fan PWM duty and mirrors
      it to all three STM32 channels.
    - **manual**: Uses per-channel duty values from config directly.
    - **off**: Sets all channels to zero duty.

    Parameters
    ----------
    config_manager:
        The application config manager for reading fan settings.
    expansion:
        The STM32 expansion board driver, or ``None`` if unavailable.
    system_info:
        The system info provider, or ``None`` if unavailable.
    """

    def __init__(
        self,
        config_manager: ConfigManager | None,
        expansion: ExpansionBoard | None,
        system_info: SystemInfo | None,
    ) -> None:
        self._config_manager = config_manager
        self._expansion = expansion
        self._system_info = system_info

        # Current applied duty per channel (hardware range 0-255).
        self._current_duty: list[int] = [0, 0, 0]
        # Current operating mode (for status reporting).
        self._current_mode: FanMode = FanMode.OFF
        # Whether the controller is in a degraded state.
        self._degraded: bool = False

        # Schmitt trigger state: tracks the last temperature band to prevent
        # rapid toggling.  None means no history yet.
        self._last_temp_band: str | None = None
        # The last temperature reading used for hysteresis decisions.
        self._last_temp: float | None = None

        # Latest metrics snapshot from the event bus (optional, used by
        # follow_temp mode to read CPU temperature without extra I2C calls).
        self._latest_metrics: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current_duty(self) -> list[int]:
        """Current duty per channel in hardware range (0-255)."""
        return list(self._current_duty)

    @property
    def current_mode(self) -> FanMode:
        """Current fan operating mode."""
        return self._current_mode

    @property
    def degraded(self) -> bool:
        """Whether the controller has entered a degraded state."""
        return self._degraded

    # ------------------------------------------------------------------
    # Metrics injection
    # ------------------------------------------------------------------

    def update_metrics(self, metrics: dict[str, Any]) -> None:
        """Update the cached metrics snapshot from the event bus.

        Parameters
        ----------
        metrics:
            A dict matching the SystemMetrics schema.
        """
        self._latest_metrics = metrics

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    async def _get_fan_config(self) -> FanConfig:
        """Read the current fan configuration from the config manager."""
        if self._config_manager is None:
            return FanConfig()
        try:
            raw = await self._config_manager.get("fan")
            return FanConfig.model_validate(raw)
        except Exception:
            logger.debug("Failed to read fan config — using defaults", exc_info=True)
            return FanConfig()

    # ------------------------------------------------------------------
    # Temperature reading
    # ------------------------------------------------------------------

    def _get_cpu_temp(self) -> float:
        """Read CPU temperature from cached metrics or system_info fallback.

        Returns 0.0 if neither source is available.
        """
        # Prefer cached metrics to avoid extra I2C / sysfs reads.
        if self._latest_metrics is not None:
            temp = self._latest_metrics.get("cpu_temp", 0.0)
            if isinstance(temp, (int, float)) and temp > 0:
                return float(temp)

        # Fallback to direct sysfs read.
        if self._system_info is not None:
            try:
                return self._system_info.get_cpu_temperature()
            except Exception:
                logger.debug("Failed to read CPU temp from system_info", exc_info=True)

        return 0.0

    # ------------------------------------------------------------------
    # Duty computation per mode
    # ------------------------------------------------------------------

    def _compute_follow_temp(self, config: FanConfig) -> list[int]:
        """Compute duty using a linear ramp with Schmitt trigger hysteresis.

        Maps 0-100% API range to 0-255 hardware range.  The ramp is linear
        between ``low_temp`` and ``high_temp``:

        - Below ``low_temp - hysteresis`` (when coming from above): low_speed
        - Above ``high_temp + hysteresis`` (when coming from below): high_speed
        - Between: linearly interpolated between low_speed and high_speed

        Returns a list of three identical duty values (all channels the same).
        """
        temp = self._get_cpu_temp()
        thresholds = config.thresholds
        low_temp = thresholds.low_temp
        high_temp = thresholds.high_temp
        hysteresis = thresholds.schmitt

        low_speed = thresholds.low_speed
        mid_speed = thresholds.mid_speed
        high_speed = thresholds.high_speed

        # Determine temperature band with Schmitt trigger hysteresis.
        # We use three bands: "low", "mid", "high".
        mid_temp = (low_temp + high_temp) / 2.0

        if self._last_temp_band is None:
            # First iteration: classify purely by temperature.
            if temp <= low_temp:
                band = "low"
            elif temp >= high_temp:
                band = "high"
            else:
                band = "mid"
        else:
            # Apply hysteresis: only transition when temperature crosses the
            # threshold by at least the hysteresis margin.
            prev_band = self._last_temp_band

            if prev_band == "low":
                if temp >= low_temp + hysteresis:
                    if temp >= high_temp + hysteresis:
                        band = "high"
                    else:
                        band = "mid"
                else:
                    band = "low"
            elif prev_band == "mid":
                if temp <= low_temp - hysteresis:
                    band = "low"
                elif temp >= high_temp + hysteresis:
                    band = "high"
                else:
                    band = "mid"
            else:  # prev_band == "high"
                if temp <= high_temp - hysteresis:
                    if temp <= low_temp - hysteresis:
                        band = "low"
                    else:
                        band = "mid"
                else:
                    band = "high"

        self._last_temp_band = band
        self._last_temp = temp

        # Map band to duty.
        if band == "low":
            duty = low_speed
        elif band == "high":
            duty = high_speed
        else:
            # Linear interpolation within the mid range.
            if high_temp > low_temp:
                fraction = (temp - low_temp) / (high_temp - low_temp)
                fraction = max(0.0, min(1.0, fraction))
                duty = int(low_speed + fraction * (high_speed - low_speed))
            else:
                duty = mid_speed

        duty = max(0, min(255, duty))
        return [duty, duty, duty]

    def _compute_follow_rpi(self, config: FanConfig) -> list[int]:
        """Read the Raspberry Pi's sysfs fan duty and map to 0-255.

        The Pi's PWM value is already in 0-255 range from sysfs.  We pass
        it through to all three STM32 channels.
        """
        rpi_duty = 0
        if self._system_info is not None:
            try:
                rpi_duty = self._system_info.get_fan_duty()
            except Exception:
                logger.debug("Failed to read RPi fan duty", exc_info=True)

        # Clamp to valid range.
        duty = max(0, min(255, rpi_duty))
        return [duty, duty, duty]

    @staticmethod
    def _compute_manual(config: FanConfig) -> list[int]:
        """Use the per-channel duty values from config (already 0-255)."""
        duties = config.manual_duty
        result: list[int] = []
        for i in range(3):
            if i < len(duties):
                result.append(max(0, min(255, duties[i])))
            else:
                result.append(0)
        return result

    @staticmethod
    def _compute_off() -> list[int]:
        """All channels to zero duty."""
        return [0, 0, 0]

    # ------------------------------------------------------------------
    # Apply duty to hardware
    # ------------------------------------------------------------------

    async def _apply_duty(self, duty: list[int]) -> None:
        """Write duty values to the STM32 expansion board.

        Always sets the STM32 to FanHwMode.MANUAL first, then writes the
        duty values.  This ensures casectl is in full control regardless
        of what mode the STM32 was previously in.
        """
        if self._expansion is None:
            self._current_duty = duty
            return

        if not self._expansion.connected:
            self._degraded = True
            self._current_duty = duty
            return

        try:
            # Always ensure STM32 is in MANUAL mode — casectl controls duty.
            await self._expansion.async_set_fan_mode(FanHwMode.MANUAL)
            await self._expansion.async_set_fan_duty(duty[0], duty[1], duty[2])
            self._current_duty = duty
            self._degraded = self._expansion.degraded
        except OSError:
            logger.warning("Failed to apply fan duty to hardware", exc_info=True)
            self._degraded = True
            self._current_duty = duty

    # ------------------------------------------------------------------
    # Main control loop tick
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Execute one iteration of the fan control loop.

        Reads the current config, computes the appropriate duty for the
        configured mode, and applies it to the hardware.
        """
        config = await self._get_fan_config()
        self._current_mode = config.mode

        if config.mode == FanMode.FOLLOW_TEMP:
            duty = self._compute_follow_temp(config)
        elif config.mode == FanMode.FOLLOW_RPI:
            duty = self._compute_follow_rpi(config)
        elif config.mode == FanMode.MANUAL:
            duty = self._compute_manual(config)
        elif config.mode == FanMode.OFF:
            duty = self._compute_off()
        else:
            # CUSTOM or unknown — treat as manual fallback.
            duty = self._compute_manual(config)

        await self._apply_duty(duty)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the fan control loop indefinitely.

        Polls every :data:`POLL_INTERVAL` seconds, reads the current config,
        computes duty, and applies it to hardware.  Catches all exceptions
        to prevent the task from dying.
        """
        logger.info("Fan control loop started (poll interval: %.1fs)", POLL_INTERVAL)

        # Ensure STM32 is in MANUAL mode on startup.
        if self._expansion is not None and self._expansion.connected:
            try:
                await self._expansion.async_set_fan_mode(FanHwMode.MANUAL)
            except OSError:
                logger.warning("Failed to set initial fan mode to MANUAL", exc_info=True)

        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                logger.info("Fan control loop cancelled")
                raise
            except Exception:
                logger.error("Fan control loop error", exc_info=True)
                self._degraded = True

            await asyncio.sleep(POLL_INTERVAL)
