"""Tests for casectl.plugins.fan.curve — multi-point fan curve interpolation engine."""

from __future__ import annotations

import pytest

from casectl.config.models import FanCurveConfig, FanCurvePoint
from casectl.plugins.fan.curve import FanCurveInterpolator


# ---------------------------------------------------------------------------
# Config schema validation tests
# ---------------------------------------------------------------------------


class TestFanCurveConfigValidation:
    """Verify FanCurveConfig Pydantic schema validation."""

    def test_default_config_is_valid(self) -> None:
        """Default FanCurveConfig has 2 points and is valid."""
        config = FanCurveConfig()
        assert len(config.points) == 2
        assert config.points[0].temp < config.points[1].temp

    def test_valid_multi_point_curve(self) -> None:
        """A well-formed 5-point curve parses correctly."""
        config = FanCurveConfig(
            name="custom",
            points=[
                FanCurvePoint(temp=25.0, duty=0),
                FanCurvePoint(temp=35.0, duty=50),
                FanCurvePoint(temp=45.0, duty=100),
                FanCurvePoint(temp=55.0, duty=180),
                FanCurvePoint(temp=65.0, duty=255),
            ],
            hysteresis=1.5,
        )
        assert len(config.points) == 5
        assert config.name == "custom"
        assert config.hysteresis == 1.5

    def test_points_are_sorted_by_temp(self) -> None:
        """Points provided out of order are sorted automatically."""
        config = FanCurveConfig(
            points=[
                FanCurvePoint(temp=60.0, duty=200),
                FanCurvePoint(temp=30.0, duty=50),
                FanCurvePoint(temp=45.0, duty=120),
            ],
        )
        temps = [p.temp for p in config.points]
        assert temps == [30.0, 45.0, 60.0]

    def test_minimum_two_points_required(self) -> None:
        """Fewer than 2 points raises a ValidationError."""
        with pytest.raises(Exception, match="at least 2 points"):
            FanCurveConfig(points=[FanCurvePoint(temp=30.0, duty=50)])

    def test_maximum_twenty_points(self) -> None:
        """More than 20 points raises a ValidationError."""
        points = [FanCurvePoint(temp=float(i), duty=min(255, i * 12)) for i in range(21)]
        with pytest.raises(Exception, match="at most 20 points"):
            FanCurveConfig(points=points)

    def test_exactly_twenty_points_is_valid(self) -> None:
        """Exactly 20 points is the maximum allowed."""
        points = [FanCurvePoint(temp=float(i * 5), duty=min(255, i * 13)) for i in range(20)]
        config = FanCurveConfig(points=points)
        assert len(config.points) == 20

    def test_duplicate_temperatures_rejected(self) -> None:
        """Two points at the same temperature raises a ValidationError."""
        with pytest.raises(Exception, match="Duplicate temperature"):
            FanCurveConfig(
                points=[
                    FanCurvePoint(temp=40.0, duty=50),
                    FanCurvePoint(temp=40.0, duty=100),
                    FanCurvePoint(temp=60.0, duty=200),
                ],
            )

    def test_duty_out_of_range_rejected(self) -> None:
        """Duty values outside 0-255 are rejected by Pydantic."""
        with pytest.raises(Exception):
            FanCurvePoint(temp=30.0, duty=300)

    def test_duty_negative_rejected(self) -> None:
        """Negative duty values are rejected."""
        with pytest.raises(Exception):
            FanCurvePoint(temp=30.0, duty=-1)

    def test_hysteresis_bounds(self) -> None:
        """Hysteresis must be 0-10."""
        with pytest.raises(Exception):
            FanCurveConfig(
                points=[
                    FanCurvePoint(temp=30.0, duty=50),
                    FanCurvePoint(temp=60.0, duty=200),
                ],
                hysteresis=15.0,
            )

    def test_yaml_round_trip(self) -> None:
        """Config serialises to dict and back correctly."""
        config = FanCurveConfig(
            name="test",
            points=[
                FanCurvePoint(temp=25.0, duty=30),
                FanCurvePoint(temp=50.0, duty=150),
                FanCurvePoint(temp=70.0, duty=255),
            ],
            hysteresis=3.0,
        )
        data = config.model_dump()
        restored = FanCurveConfig.model_validate(data)
        assert restored.name == config.name
        assert len(restored.points) == len(config.points)
        for orig, rest in zip(config.points, restored.points):
            assert orig.temp == rest.temp
            assert orig.duty == rest.duty


# ---------------------------------------------------------------------------
# Raw interpolation (no hysteresis)
# ---------------------------------------------------------------------------


class TestInterpolateRaw:
    """Test piecewise-linear interpolation without hysteresis."""

    @pytest.fixture()
    def simple_curve(self) -> FanCurveInterpolator:
        """A simple 3-point curve: 30C->50, 50C->150, 70C->255."""
        config = FanCurveConfig(
            points=[
                FanCurvePoint(temp=30.0, duty=50),
                FanCurvePoint(temp=50.0, duty=150),
                FanCurvePoint(temp=70.0, duty=255),
            ],
            hysteresis=0.0,  # Disable hysteresis for raw tests.
        )
        return FanCurveInterpolator(config)

    def test_below_first_point(self, simple_curve: FanCurveInterpolator) -> None:
        """Temperature below the first point returns the first point's duty."""
        assert simple_curve.interpolate_raw(20.0) == 50

    def test_at_first_point(self, simple_curve: FanCurveInterpolator) -> None:
        """Temperature exactly at the first point."""
        assert simple_curve.interpolate_raw(30.0) == 50

    def test_at_last_point(self, simple_curve: FanCurveInterpolator) -> None:
        """Temperature exactly at the last point."""
        assert simple_curve.interpolate_raw(70.0) == 255

    def test_above_last_point(self, simple_curve: FanCurveInterpolator) -> None:
        """Temperature above the last point returns the last point's duty."""
        assert simple_curve.interpolate_raw(90.0) == 255

    def test_midpoint_first_segment(self, simple_curve: FanCurveInterpolator) -> None:
        """40C is midpoint of 30->50 segment: duty = 50 + 0.5*(150-50) = 100."""
        assert simple_curve.interpolate_raw(40.0) == 100

    def test_midpoint_second_segment(self, simple_curve: FanCurveInterpolator) -> None:
        """60C is midpoint of 50->70 segment: duty = 150 + 0.5*(255-150) = 202."""
        assert simple_curve.interpolate_raw(60.0) == 202

    def test_quarter_point(self, simple_curve: FanCurveInterpolator) -> None:
        """35C is 25% of first segment: duty = 50 + 0.25*100 = 75."""
        assert simple_curve.interpolate_raw(35.0) == 75

    def test_three_quarter_point(self, simple_curve: FanCurveInterpolator) -> None:
        """45C is 75% of first segment: duty = 50 + 0.75*100 = 125."""
        assert simple_curve.interpolate_raw(45.0) == 125

    def test_at_interior_point(self, simple_curve: FanCurveInterpolator) -> None:
        """Exactly at 50C (a defined point): duty = 150."""
        # 50.0 >= temps[1] so bisect_right returns index 2
        # but 50.0 is also <= temps[1], so it should return 150
        # Actually at exactly 50.0, bisect_right finds index 2
        # segment is [1, 2]: t0=50, t1=70, fraction=0 -> duty=150
        assert simple_curve.interpolate_raw(50.0) == 150

    def test_monotonic_increase(self, simple_curve: FanCurveInterpolator) -> None:
        """Duty increases monotonically across the range for a monotonic curve."""
        prev_duty = -1
        for temp_10x in range(200, 800):  # 20.0 to 79.9
            temp = temp_10x / 10.0
            duty = simple_curve.interpolate_raw(temp)
            assert duty >= prev_duty, f"Duty decreased at {temp}C: {duty} < {prev_duty}"
            prev_duty = duty


class TestInterpolateRawTwoPoints:
    """Test with minimal 2-point curve."""

    def test_two_point_linear(self) -> None:
        """Simple 2-point curve: 0C->0, 100C->255."""
        config = FanCurveConfig(
            points=[
                FanCurvePoint(temp=0.0, duty=0),
                FanCurvePoint(temp=100.0, duty=255),
            ],
            hysteresis=0.0,
        )
        interp = FanCurveInterpolator(config)

        assert interp.interpolate_raw(-10.0) == 0
        assert interp.interpolate_raw(0.0) == 0
        assert interp.interpolate_raw(50.0) == 127  # int(127.5) = 127
        assert interp.interpolate_raw(100.0) == 255
        assert interp.interpolate_raw(110.0) == 255


class TestInterpolateRawStepCurve:
    """Test with a step-like curve (rapid duty change over small temp range)."""

    def test_steep_segment(self) -> None:
        """Step-like curve: 39C->50, 40C->200, 41C->210."""
        config = FanCurveConfig(
            points=[
                FanCurvePoint(temp=39.0, duty=50),
                FanCurvePoint(temp=40.0, duty=200),
                FanCurvePoint(temp=41.0, duty=210),
            ],
            hysteresis=0.0,
        )
        interp = FanCurveInterpolator(config)

        assert interp.interpolate_raw(39.0) == 50
        assert interp.interpolate_raw(39.5) == 125  # midpoint of 50->200
        assert interp.interpolate_raw(40.0) == 200
        assert interp.interpolate_raw(40.5) == 205  # midpoint of 200->210
        assert interp.interpolate_raw(41.0) == 210


class TestInterpolateRawFlatSegment:
    """Test with a flat (constant duty) segment."""

    def test_flat_middle(self) -> None:
        """Curve with a flat region: 30C->100, 50C->100, 70C->200."""
        config = FanCurveConfig(
            points=[
                FanCurvePoint(temp=30.0, duty=100),
                FanCurvePoint(temp=50.0, duty=100),
                FanCurvePoint(temp=70.0, duty=200),
            ],
            hysteresis=0.0,
        )
        interp = FanCurveInterpolator(config)

        assert interp.interpolate_raw(30.0) == 100
        assert interp.interpolate_raw(40.0) == 100  # flat segment
        assert interp.interpolate_raw(50.0) == 100
        assert interp.interpolate_raw(60.0) == 150  # midpoint of 100->200


# ---------------------------------------------------------------------------
# Hysteresis behaviour
# ---------------------------------------------------------------------------


class TestHysteresis:
    """Test hysteresis prevents rapid duty changes."""

    @pytest.fixture()
    def hyst_interp(self) -> FanCurveInterpolator:
        """A curve with 2.0C hysteresis."""
        config = FanCurveConfig(
            points=[
                FanCurvePoint(temp=30.0, duty=50),
                FanCurvePoint(temp=50.0, duty=150),
                FanCurvePoint(temp=70.0, duty=255),
            ],
            hysteresis=2.0,
        )
        return FanCurveInterpolator(config)

    def test_first_call_always_returns_raw(self, hyst_interp: FanCurveInterpolator) -> None:
        """First compute() call returns the raw interpolated value."""
        duty = hyst_interp.compute(40.0)
        assert duty == hyst_interp.interpolate_raw(40.0)

    def test_small_change_within_hysteresis(self, hyst_interp: FanCurveInterpolator) -> None:
        """A 1C change with 2C hysteresis should not change duty."""
        first = hyst_interp.compute(40.0)
        second = hyst_interp.compute(41.0)  # 1C < 2C hysteresis
        assert second == first

    def test_large_change_beyond_hysteresis(self, hyst_interp: FanCurveInterpolator) -> None:
        """A 3C change with 2C hysteresis should update duty."""
        first = hyst_interp.compute(40.0)
        second = hyst_interp.compute(43.0)  # 3C >= 2C hysteresis
        assert second == hyst_interp.interpolate_raw(43.0)
        assert second != first

    def test_oscillation_suppressed(self, hyst_interp: FanCurveInterpolator) -> None:
        """Oscillating +/-1C around 40C should hold steady."""
        duty0 = hyst_interp.compute(40.0)
        duties = []
        for temp in [40.5, 39.5, 40.8, 39.2, 40.3]:
            duties.append(hyst_interp.compute(temp))
        # All should be the same as the first duty
        assert all(d == duty0 for d in duties)

    def test_reset_clears_hysteresis(self, hyst_interp: FanCurveInterpolator) -> None:
        """After reset(), the next call should be treated as the first."""
        hyst_interp.compute(40.0)
        hyst_interp.reset()
        assert hyst_interp.last_duty is None
        # Next call should compute fresh
        duty = hyst_interp.compute(60.0)
        assert duty == hyst_interp.interpolate_raw(60.0)

    def test_zero_hysteresis_always_updates(self) -> None:
        """With hysteresis=0, every call updates to raw value."""
        config = FanCurveConfig(
            points=[
                FanCurvePoint(temp=30.0, duty=50),
                FanCurvePoint(temp=70.0, duty=200),
            ],
            hysteresis=0.0,
        )
        interp = FanCurveInterpolator(config)
        d1 = interp.compute(40.0)
        d2 = interp.compute(40.1)
        # 0.1 >= 0.0 hysteresis, so it updates
        assert d2 == interp.interpolate_raw(40.1)

    def test_gradual_rise_updates_at_threshold(self, hyst_interp: FanCurveInterpolator) -> None:
        """Gradually rising temp crosses hysteresis and updates duty."""
        d1 = hyst_interp.compute(40.0)
        # Still within hysteresis
        d2 = hyst_interp.compute(41.0)
        assert d2 == d1
        # Exactly at hysteresis boundary
        d3 = hyst_interp.compute(42.0)
        assert d3 == hyst_interp.interpolate_raw(42.0)


# ---------------------------------------------------------------------------
# Properties and state
# ---------------------------------------------------------------------------


class TestInterpolatorState:
    """Test interpolator properties and state management."""

    def test_config_property(self) -> None:
        config = FanCurveConfig()
        interp = FanCurveInterpolator(config)
        assert interp.config is config

    def test_last_duty_initially_none(self) -> None:
        config = FanCurveConfig()
        interp = FanCurveInterpolator(config)
        assert interp.last_duty is None

    def test_last_duty_after_compute(self) -> None:
        config = FanCurveConfig(hysteresis=0.0)
        interp = FanCurveInterpolator(config)
        duty = interp.compute(40.0)
        assert interp.last_duty == duty


# ---------------------------------------------------------------------------
# Integration with FanConfig
# ---------------------------------------------------------------------------


class TestFanConfigCurveIntegration:
    """Test that FanCurveConfig integrates properly with FanConfig."""

    def test_fan_config_has_default_curve(self) -> None:
        from casectl.config.models import FanConfig
        config = FanConfig()
        assert config.curve is not None
        assert len(config.curve.points) == 2

    def test_fan_config_with_custom_curve(self) -> None:
        from casectl.config.models import FanConfig, FanMode
        config = FanConfig(
            mode=FanMode.CUSTOM,
            curve=FanCurveConfig(
                name="gaming",
                points=[
                    FanCurvePoint(temp=25.0, duty=30),
                    FanCurvePoint(temp=40.0, duty=80),
                    FanCurvePoint(temp=55.0, duty=160),
                    FanCurvePoint(temp=70.0, duty=255),
                ],
                hysteresis=2.5,
            ),
        )
        assert config.mode == FanMode.CUSTOM
        assert config.curve.name == "gaming"
        assert len(config.curve.points) == 4

    def test_fan_config_round_trip(self) -> None:
        from casectl.config.models import FanConfig, FanMode
        config = FanConfig(
            mode=FanMode.CUSTOM,
            curve=FanCurveConfig(
                name="silent",
                points=[
                    FanCurvePoint(temp=30.0, duty=20),
                    FanCurvePoint(temp=60.0, duty=100),
                    FanCurvePoint(temp=80.0, duty=200),
                ],
            ),
        )
        data = config.model_dump(mode="python")
        restored = FanConfig.model_validate(data)
        assert restored.mode == FanMode.CUSTOM
        assert restored.curve.name == "silent"
        assert len(restored.curve.points) == 3


# ---------------------------------------------------------------------------
# Integration with FanController (CUSTOM mode)
# ---------------------------------------------------------------------------


class TestFanControllerCustomMode:
    """Test that FanController uses the curve in CUSTOM mode."""

    async def test_custom_mode_uses_curve(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from casectl.config.models import FanConfig, FanMode
        from casectl.plugins.fan.controller import FanController

        config = FanConfig(
            mode=FanMode.CUSTOM,
            curve=FanCurveConfig(
                points=[
                    FanCurvePoint(temp=30.0, duty=50),
                    FanCurvePoint(temp=70.0, duty=250),
                ],
                hysteresis=0.0,
            ),
        )
        config_manager = AsyncMock()
        config_manager.get = AsyncMock(return_value=config.model_dump(mode="python"))
        expansion = MagicMock()
        expansion.connected = True
        expansion.degraded = False
        expansion.async_set_fan_mode = AsyncMock()
        expansion.async_set_fan_duty = AsyncMock()

        ctrl = FanController(
            config_manager=config_manager,
            expansion=expansion,
            system_info=None,
        )
        # Set temperature to 50C: midpoint of 30-70 range
        # duty = 50 + 0.5 * 200 = 150
        ctrl.update_metrics({"cpu_temp": 50.0})
        await ctrl.tick()

        assert ctrl.current_mode == FanMode.CUSTOM
        assert ctrl.current_duty == [150, 150, 150]

    async def test_custom_mode_below_range(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from casectl.config.models import FanConfig, FanMode
        from casectl.plugins.fan.controller import FanController

        config = FanConfig(
            mode=FanMode.CUSTOM,
            curve=FanCurveConfig(
                points=[
                    FanCurvePoint(temp=30.0, duty=50),
                    FanCurvePoint(temp=70.0, duty=250),
                ],
                hysteresis=0.0,
            ),
        )
        config_manager = AsyncMock()
        config_manager.get = AsyncMock(return_value=config.model_dump(mode="python"))

        ctrl = FanController(
            config_manager=config_manager,
            expansion=None,
            system_info=None,
        )
        ctrl.update_metrics({"cpu_temp": 20.0})
        await ctrl.tick()

        assert ctrl.current_duty == [50, 50, 50]

    async def test_custom_mode_above_range(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from casectl.config.models import FanConfig, FanMode
        from casectl.plugins.fan.controller import FanController

        config = FanConfig(
            mode=FanMode.CUSTOM,
            curve=FanCurveConfig(
                points=[
                    FanCurvePoint(temp=30.0, duty=50),
                    FanCurvePoint(temp=70.0, duty=250),
                ],
                hysteresis=0.0,
            ),
        )
        config_manager = AsyncMock()
        config_manager.get = AsyncMock(return_value=config.model_dump(mode="python"))

        ctrl = FanController(
            config_manager=config_manager,
            expansion=None,
            system_info=None,
        )
        ctrl.update_metrics({"cpu_temp": 90.0})
        await ctrl.tick()

        assert ctrl.current_duty == [250, 250, 250]

    async def test_custom_mode_curve_reinit_on_name_change(self) -> None:
        """Changing curve name should re-initialise the interpolator."""
        from unittest.mock import AsyncMock
        from casectl.config.models import FanConfig, FanMode
        from casectl.plugins.fan.controller import FanController

        config1 = FanConfig(
            mode=FanMode.CUSTOM,
            curve=FanCurveConfig(
                name="quiet",
                points=[
                    FanCurvePoint(temp=30.0, duty=20),
                    FanCurvePoint(temp=70.0, duty=100),
                ],
                hysteresis=0.0,
            ),
        )
        config2 = FanConfig(
            mode=FanMode.CUSTOM,
            curve=FanCurveConfig(
                name="performance",
                points=[
                    FanCurvePoint(temp=30.0, duty=100),
                    FanCurvePoint(temp=70.0, duty=255),
                ],
                hysteresis=0.0,
            ),
        )

        config_manager = AsyncMock()
        config_manager.get = AsyncMock(return_value=config1.model_dump(mode="python"))

        ctrl = FanController(
            config_manager=config_manager,
            expansion=None,
            system_info=None,
        )
        ctrl.update_metrics({"cpu_temp": 50.0})
        await ctrl.tick()
        # Midpoint of quiet: 20 + 0.5 * 80 = 60
        assert ctrl.current_duty == [60, 60, 60]

        # Switch to performance curve
        config_manager.get = AsyncMock(return_value=config2.model_dump(mode="python"))
        await ctrl.tick()
        # Midpoint of performance: 100 + 0.5 * 155 = 177
        assert ctrl.current_duty == [177, 177, 177]
