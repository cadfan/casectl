"""Tests for casectl.plugins.fan.controller — FanController logic."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from casectl.config.models import FanConfig, FanMode, FanThresholds
from casectl.plugins.fan.controller import FanController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_controller(
    *,
    config: FanConfig | None = None,
    expansion: MagicMock | None = None,
    system_info: MagicMock | None = None,
) -> FanController:
    """Create a FanController with mocked dependencies.

    The config_manager is mocked to return the given FanConfig (or default).
    """
    cfg = config or FanConfig()
    config_manager = AsyncMock()
    config_manager.get = AsyncMock(return_value=cfg.model_dump(mode="python"))

    if expansion is None:
        expansion = MagicMock()
        expansion.connected = True
        expansion.degraded = False
        expansion.async_set_fan_mode = AsyncMock()
        expansion.async_set_fan_duty = AsyncMock()

    if system_info is None:
        system_info = MagicMock()
        system_info.get_cpu_temperature.return_value = 40.0
        system_info.get_fan_duty.return_value = 128

    ctrl = FanController(
        config_manager=config_manager,
        expansion=expansion,
        system_info=system_info,
    )
    return ctrl


# ---------------------------------------------------------------------------
# FOLLOW_TEMP mode — normal range
# ---------------------------------------------------------------------------


class TestFollowTempNormal:
    """Verify proportional duty when temperature is within range."""

    async def test_follow_temp_mid_range(self) -> None:
        """40C with default thresholds (30-50) -> mid-range duty.

        At 40C, fraction = (40-30)/(50-30) = 0.5
        duty = 75 + 0.5 * (175 - 75) = 125
        """
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 40.0})

        await ctrl.tick()

        assert ctrl.current_mode == FanMode.FOLLOW_TEMP
        duty = ctrl.current_duty
        # At 40C, mid-range, first tick classifies as "mid"
        # fraction = (40-30)/(50-30) = 0.5 -> duty = 75 + 0.5*100 = 125
        assert duty == [125, 125, 125]

    async def test_follow_temp_quarter_range(self) -> None:
        """35C -> fraction = (35-30)/(50-30) = 0.25 -> duty = 75 + 0.25*100 = 100."""
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 35.0})

        await ctrl.tick()

        duty = ctrl.current_duty
        # 35C is in mid band on first tick, fraction = 0.25
        assert duty == [100, 100, 100]

    async def test_follow_temp_three_quarter_range(self) -> None:
        """45C -> fraction = (45-30)/(50-30) = 0.75 -> duty = 75 + 0.75*100 = 150."""
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 45.0})

        await ctrl.tick()

        assert ctrl.current_duty == [150, 150, 150]


# ---------------------------------------------------------------------------
# FOLLOW_TEMP — below low threshold
# ---------------------------------------------------------------------------


class TestFollowTempBelowLow:
    """Verify duty when temperature is below the low threshold."""

    async def test_temp_below_low(self) -> None:
        """20C with low_temp=30 -> 'low' band -> low_speed=75."""
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 20.0})

        await ctrl.tick()

        assert ctrl.current_duty == [75, 75, 75]

    async def test_temp_exactly_at_low(self) -> None:
        """Exactly at low_temp -> 'low' band."""
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 30.0})

        await ctrl.tick()

        assert ctrl.current_duty == [75, 75, 75]

    async def test_temp_zero(self) -> None:
        """0C -> always 'low' band.

        When cpu_temp is 0.0, _get_cpu_temp() skips the cached metrics
        (because 0.0 fails the ``temp > 0`` guard) and falls back to
        system_info.  We must set that fallback to 0.0 as well.
        """
        system_info = MagicMock()
        system_info.get_cpu_temperature.return_value = 0.0
        system_info.get_fan_duty.return_value = 0

        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config, system_info=system_info)
        ctrl.update_metrics({"cpu_temp": 0.0})

        await ctrl.tick()

        assert ctrl.current_duty == [75, 75, 75]


# ---------------------------------------------------------------------------
# FOLLOW_TEMP — above high threshold
# ---------------------------------------------------------------------------


class TestFollowTempAboveHigh:
    """Verify duty when temperature is above the high threshold."""

    async def test_temp_above_high(self) -> None:
        """60C with high_temp=50 -> 'high' band -> high_speed=175."""
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 60.0})

        await ctrl.tick()

        assert ctrl.current_duty == [175, 175, 175]

    async def test_temp_exactly_at_high(self) -> None:
        """Exactly at high_temp -> 'high' band."""
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 50.0})

        await ctrl.tick()

        assert ctrl.current_duty == [175, 175, 175]

    async def test_temp_very_high(self) -> None:
        """90C -> always 'high' band, duty clamped to high_speed."""
        config = FanConfig(mode=FanMode.FOLLOW_TEMP)
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 90.0})

        await ctrl.tick()

        assert ctrl.current_duty == [175, 175, 175]


# ---------------------------------------------------------------------------
# Hysteresis prevents flapping
# ---------------------------------------------------------------------------


class TestHysteresisPreventsFlapping:
    """Verify Schmitt trigger hysteresis prevents rapid band changes."""

    async def test_oscillation_around_low_threshold(self) -> None:
        """Oscillate +/-2C around low_temp=30 with schmitt=3.

        Starting from 'mid' band at 32C, dropping to 28C (which is above
        low_temp - schmitt = 27) should NOT transition to 'low'.
        """
        config = FanConfig(
            mode=FanMode.FOLLOW_TEMP,
            thresholds=FanThresholds(
                low_temp=30, high_temp=50, schmitt=3,
                low_speed=75, mid_speed=125, high_speed=175,
            ),
        )
        ctrl = _make_controller(config=config)

        # First tick at 32C -> classifies as 'mid'
        ctrl.update_metrics({"cpu_temp": 32.0})
        await ctrl.tick()
        mid_duty = ctrl.current_duty[0]

        # Drop to 28C -> still above (low_temp - schmitt) = 27, so stays 'mid'
        ctrl.update_metrics({"cpu_temp": 28.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "mid"

        # Back up to 32C -> still 'mid'
        ctrl.update_metrics({"cpu_temp": 32.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "mid"

    async def test_transition_to_low_requires_crossing_hysteresis(self) -> None:
        """Must drop below low_temp - schmitt to transition from mid to low."""
        config = FanConfig(
            mode=FanMode.FOLLOW_TEMP,
            thresholds=FanThresholds(
                low_temp=30, high_temp=50, schmitt=3,
                low_speed=75, mid_speed=125, high_speed=175,
            ),
        )
        ctrl = _make_controller(config=config)

        # Start in 'mid' at 35C
        ctrl.update_metrics({"cpu_temp": 35.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "mid"

        # Drop to 26C -> below (30 - 3) = 27, should transition to 'low'
        ctrl.update_metrics({"cpu_temp": 26.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "low"
        assert ctrl.current_duty == [75, 75, 75]

    async def test_transition_to_high_requires_crossing_hysteresis(self) -> None:
        """Must rise above high_temp + schmitt to transition from mid to high."""
        config = FanConfig(
            mode=FanMode.FOLLOW_TEMP,
            thresholds=FanThresholds(
                low_temp=30, high_temp=50, schmitt=3,
                low_speed=75, mid_speed=125, high_speed=175,
            ),
        )
        ctrl = _make_controller(config=config)

        # Start in 'mid' at 48C
        ctrl.update_metrics({"cpu_temp": 48.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "mid"

        # Rise to 52C -> above 50 but below 50+3=53, stays 'mid'
        ctrl.update_metrics({"cpu_temp": 52.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "mid"

        # Rise to 54C -> above 53, transitions to 'high'
        ctrl.update_metrics({"cpu_temp": 54.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "high"
        assert ctrl.current_duty == [175, 175, 175]

    async def test_hysteresis_on_return_from_high(self) -> None:
        """Coming down from 'high' band requires dropping below high_temp - schmitt."""
        config = FanConfig(
            mode=FanMode.FOLLOW_TEMP,
            thresholds=FanThresholds(
                low_temp=30, high_temp=50, schmitt=3,
                low_speed=75, mid_speed=125, high_speed=175,
            ),
        )
        ctrl = _make_controller(config=config)

        # Start at 55C -> 'high'
        ctrl.update_metrics({"cpu_temp": 55.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "high"

        # Drop to 48C -> above (50 - 3) = 47, stays 'high'
        ctrl.update_metrics({"cpu_temp": 48.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "high"

        # Drop to 46C -> below 47, transitions to 'mid'
        ctrl.update_metrics({"cpu_temp": 46.0})
        await ctrl.tick()
        assert ctrl._last_temp_band == "mid"


# ---------------------------------------------------------------------------
# MANUAL mode
# ---------------------------------------------------------------------------


class TestManualMode:
    """Verify manual mode uses config duty values directly."""

    async def test_manual_mode_uses_config_duty(self) -> None:
        config = FanConfig(mode=FanMode.MANUAL, manual_duty=[100, 150, 200])
        ctrl = _make_controller(config=config)
        await ctrl.tick()
        assert ctrl.current_duty == [100, 150, 200]

    def test_manual_mode_clamps_values(self) -> None:
        """_compute_manual clamps out-of-range values to 0-255.

        The Pydantic validator rejects out-of-range values at construction
        time, so we test the static clamping method directly with a
        model_construct bypass.
        """
        config = FanConfig.model_construct(
            mode=FanMode.MANUAL,
            manual_duty=[300, -10, 128],
            thresholds=FanThresholds(),
            rpi_follow_min=0,
            rpi_follow_max=255,
            is_run_on_startup=True,
        )
        result = FanController._compute_manual(config)
        assert result == [255, 0, 128]

    async def test_manual_mode_short_list(self) -> None:
        """A short manual_duty list pads with zeros."""
        config = FanConfig(mode=FanMode.MANUAL, manual_duty=[100])
        ctrl = _make_controller(config=config)
        await ctrl.tick()
        assert ctrl.current_duty == [100, 0, 0]

    async def test_manual_mode_applies_to_hardware(self) -> None:
        expansion = MagicMock()
        expansion.connected = True
        expansion.degraded = False
        expansion.async_set_fan_mode = AsyncMock()
        expansion.async_set_fan_duty = AsyncMock()

        config = FanConfig(mode=FanMode.MANUAL, manual_duty=[80, 90, 100])
        ctrl = _make_controller(config=config, expansion=expansion)
        await ctrl.tick()

        expansion.async_set_fan_duty.assert_called_once_with(80, 90, 100)


# ---------------------------------------------------------------------------
# OFF mode
# ---------------------------------------------------------------------------


class TestOffMode:
    """Verify OFF mode sets all channels to 0."""

    async def test_off_mode_all_zero(self) -> None:
        config = FanConfig(mode=FanMode.OFF)
        ctrl = _make_controller(config=config)
        await ctrl.tick()
        assert ctrl.current_duty == [0, 0, 0]

    async def test_off_mode_applies_to_hardware(self) -> None:
        expansion = MagicMock()
        expansion.connected = True
        expansion.degraded = False
        expansion.async_set_fan_mode = AsyncMock()
        expansion.async_set_fan_duty = AsyncMock()

        config = FanConfig(mode=FanMode.OFF)
        ctrl = _make_controller(config=config, expansion=expansion)
        await ctrl.tick()

        expansion.async_set_fan_duty.assert_called_once_with(0, 0, 0)


# ---------------------------------------------------------------------------
# Duty percentage to hardware conversion
# ---------------------------------------------------------------------------


class TestDutyConversion:
    """Verify that duty values map correctly to the 0-255 hardware range."""

    async def test_50_percent_maps_to_127(self) -> None:
        """A 50% fan curve position should yield approximately 127.

        With default thresholds: low_speed=75, high_speed=175,
        50% fraction -> 75 + 0.5*100 = 125. To get exactly 127 we
        need temp at fraction (127-75)/100 = 0.52 -> temp = 30 + 0.52*20 = 40.4C.
        """
        config = FanConfig(
            mode=FanMode.FOLLOW_TEMP,
            thresholds=FanThresholds(
                low_temp=30, high_temp=50, schmitt=3,
                low_speed=0, mid_speed=127, high_speed=255,
            ),
        )
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 40.0})  # 50% of range

        await ctrl.tick()

        duty = ctrl.current_duty[0]
        # fraction = (40-30)/(50-30) = 0.5
        # duty = 0 + 0.5 * 255 = 127.5 -> int = 127
        assert duty == 127

    async def test_zero_duty_at_low(self) -> None:
        """At low band, duty = low_speed."""
        config = FanConfig(
            mode=FanMode.FOLLOW_TEMP,
            thresholds=FanThresholds(
                low_temp=30, high_temp=50, schmitt=3,
                low_speed=0, mid_speed=127, high_speed=255,
            ),
        )
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 20.0})

        await ctrl.tick()

        assert ctrl.current_duty == [0, 0, 0]

    async def test_max_duty_at_high(self) -> None:
        """At high band, duty = high_speed."""
        config = FanConfig(
            mode=FanMode.FOLLOW_TEMP,
            thresholds=FanThresholds(
                low_temp=30, high_temp=50, schmitt=3,
                low_speed=0, mid_speed=127, high_speed=255,
            ),
        )
        ctrl = _make_controller(config=config)
        ctrl.update_metrics({"cpu_temp": 60.0})

        await ctrl.tick()

        assert ctrl.current_duty == [255, 255, 255]


# ---------------------------------------------------------------------------
# FOLLOW_RPI mode
# ---------------------------------------------------------------------------


class TestFollowRpiMode:
    """Verify FOLLOW_RPI reads the Pi's fan duty."""

    async def test_follow_rpi_reads_system_info(self) -> None:
        system_info = MagicMock()
        system_info.get_fan_duty.return_value = 200

        config = FanConfig(mode=FanMode.FOLLOW_RPI)
        ctrl = _make_controller(config=config, system_info=system_info)
        await ctrl.tick()

        assert ctrl.current_duty == [200, 200, 200]

    async def test_follow_rpi_clamps_value(self) -> None:
        system_info = MagicMock()
        system_info.get_fan_duty.return_value = 300  # over max

        config = FanConfig(mode=FanMode.FOLLOW_RPI)
        ctrl = _make_controller(config=config, system_info=system_info)
        await ctrl.tick()

        assert ctrl.current_duty == [255, 255, 255]

    async def test_follow_rpi_without_system_info(self) -> None:
        config = FanConfig(mode=FanMode.FOLLOW_RPI)
        ctrl = FanController(
            config_manager=AsyncMock(
                get=AsyncMock(return_value=config.model_dump(mode="python"))
            ),
            expansion=None,
            system_info=None,
        )
        await ctrl.tick()
        assert ctrl.current_duty == [0, 0, 0]


# ---------------------------------------------------------------------------
# Degraded mode / hardware unavailable
# ---------------------------------------------------------------------------


class TestDegradedState:
    """Verify degraded state tracking when hardware is unavailable."""

    async def test_no_expansion_still_computes_duty(self) -> None:
        """When expansion is None, duty is computed but not applied."""
        config = FanConfig(mode=FanMode.MANUAL, manual_duty=[100, 100, 100])
        ctrl = FanController(
            config_manager=AsyncMock(
                get=AsyncMock(return_value=config.model_dump(mode="python"))
            ),
            expansion=None,
            system_info=None,
        )
        await ctrl.tick()
        assert ctrl.current_duty == [100, 100, 100]

    async def test_disconnected_expansion_sets_degraded(self) -> None:
        expansion = MagicMock()
        expansion.connected = False
        expansion.degraded = False

        config = FanConfig(mode=FanMode.OFF)
        ctrl = _make_controller(config=config, expansion=expansion)
        await ctrl.tick()

        assert ctrl.degraded is True

    async def test_hardware_oserror_sets_degraded(self) -> None:
        expansion = MagicMock()
        expansion.connected = True
        expansion.degraded = False
        expansion.async_set_fan_mode = AsyncMock(side_effect=OSError("I2C fail"))
        expansion.async_set_fan_duty = AsyncMock()

        config = FanConfig(mode=FanMode.MANUAL, manual_duty=[50, 50, 50])
        ctrl = _make_controller(config=config, expansion=expansion)
        await ctrl.tick()

        assert ctrl.degraded is True
