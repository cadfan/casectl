"""Multi-point fan curve interpolation engine.

Provides piecewise-linear interpolation between user-defined temperature-to-duty
curve points, with optional hysteresis to prevent rapid duty changes when the
temperature oscillates near a curve point.
"""

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from casectl.config.models import FanCurveConfig


class FanCurveInterpolator:
    """Interpolates PWM duty from temperature using a multi-point fan curve.

    The curve is a sorted list of (temp, duty) points.  Between two adjacent
    points, duty is linearly interpolated.  Below the first point, the first
    point's duty is returned.  Above the last point, the last point's duty
    is returned.

    Hysteresis is applied by tracking the last computed duty and only changing
    it when the temperature moves beyond the hysteresis band.  This prevents
    rapid toggling when temperature sits right at a curve point.

    Parameters
    ----------
    curve_config:
        The FanCurveConfig containing points and hysteresis settings.
    """

    def __init__(self, curve_config: FanCurveConfig) -> None:
        self._config = curve_config
        # Pre-extract sorted temps and duties for fast lookup.
        self._temps = [p.temp for p in curve_config.points]
        self._duties = [p.duty for p in curve_config.points]
        self._hysteresis = curve_config.hysteresis

        # State for hysteresis tracking.
        self._last_duty: int | None = None
        self._last_temp: float | None = None

    @property
    def config(self) -> FanCurveConfig:
        """The underlying curve configuration."""
        return self._config

    @property
    def last_duty(self) -> int | None:
        """The last computed duty value, or None if never called."""
        return self._last_duty

    def reset(self) -> None:
        """Reset hysteresis state (e.g. after curve config change)."""
        self._last_duty = None
        self._last_temp = None

    def interpolate_raw(self, temp: float) -> int:
        """Compute duty for the given temperature without hysteresis.

        This is a pure piecewise-linear interpolation with clamping at the
        endpoints.

        Parameters
        ----------
        temp:
            Current temperature in degrees Celsius.

        Returns
        -------
        int
            PWM duty cycle in the range 0-255.
        """
        temps = self._temps
        duties = self._duties

        # Below first point
        if temp <= temps[0]:
            return duties[0]

        # Above last point
        if temp >= temps[-1]:
            return duties[-1]

        # Find the segment: temps[i-1] <= temp < temps[i]
        i = bisect.bisect_right(temps, temp)
        # i is the index of the first element > temp, so segment is [i-1, i]
        t0, t1 = temps[i - 1], temps[i]
        d0, d1 = duties[i - 1], duties[i]

        # Linear interpolation
        if t1 == t0:
            return d0
        fraction = (temp - t0) / (t1 - t0)
        duty = d0 + fraction * (d1 - d0)
        return max(0, min(255, int(duty)))

    def compute(self, temp: float) -> int:
        """Compute duty for the given temperature with hysteresis.

        If the temperature has not moved beyond the hysteresis band since the
        last call, the previous duty value is returned unchanged.  This
        prevents rapid fluctuation when temperature hovers near a curve point.

        Parameters
        ----------
        temp:
            Current temperature in degrees Celsius.

        Returns
        -------
        int
            PWM duty cycle in the range 0-255.
        """
        raw_duty = self.interpolate_raw(temp)

        if self._last_duty is None or self._last_temp is None:
            # First call — no hysteresis to apply.
            self._last_duty = raw_duty
            self._last_temp = temp
            return raw_duty

        # Only update if temperature has moved beyond the hysteresis band.
        if abs(temp - self._last_temp) >= self._hysteresis:
            self._last_duty = raw_duty
            self._last_temp = temp
            return raw_duty

        # Within hysteresis band — return previous duty.
        return self._last_duty
