"""Tests for casectl.config.models — Pydantic configuration schemas."""

from __future__ import annotations

import pytest

from casectl.config.models import (
    AlertConfig,
    CaseCtlConfig,
    FanConfig,
    FanMode,
    FanThresholds,
    LedConfig,
    LedMode,
    OledConfig,
    OledScreenConfig,
    ServiceConfig,
    SystemMetrics,
)


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Verify CaseCtlConfig() produces expected defaults."""

    def test_default_config_creates_all_sections(self) -> None:
        cfg = CaseCtlConfig()
        assert isinstance(cfg.fan, FanConfig)
        assert isinstance(cfg.led, LedConfig)
        assert isinstance(cfg.oled, OledConfig)
        assert isinstance(cfg.service, ServiceConfig)
        assert isinstance(cfg.alerts, AlertConfig)
        assert isinstance(cfg.plugins, dict)

    def test_default_fan_mode_is_follow_temp(self) -> None:
        cfg = CaseCtlConfig()
        assert cfg.fan.mode == FanMode.FOLLOW_TEMP

    def test_default_led_mode_is_rainbow(self) -> None:
        cfg = CaseCtlConfig()
        assert cfg.led.mode == LedMode.RAINBOW

    def test_default_api_port(self) -> None:
        cfg = CaseCtlConfig()
        assert cfg.service.api_port == 8420

    def test_default_api_host(self) -> None:
        cfg = CaseCtlConfig()
        assert cfg.service.api_host == "127.0.0.1"

    def test_default_plugins_empty(self) -> None:
        cfg = CaseCtlConfig()
        assert cfg.plugins == {}

    def test_default_alerts_disabled(self) -> None:
        cfg = CaseCtlConfig()
        assert cfg.alerts.enabled is False

    def test_default_fan_manual_duty(self) -> None:
        cfg = CaseCtlConfig()
        assert cfg.fan.manual_duty == [75, 75, 75]

    def test_default_fan_thresholds(self) -> None:
        cfg = CaseCtlConfig()
        t = cfg.fan.thresholds
        assert t.low_temp == 30
        assert t.high_temp == 50
        assert t.schmitt == 3
        assert t.low_speed == 75
        assert t.mid_speed == 125
        assert t.high_speed == 175


# ---------------------------------------------------------------------------
# FanConfig serialization round-trip
# ---------------------------------------------------------------------------


class TestFanConfigSerialization:
    """Verify model_dump / model_validate round-trip for FanConfig."""

    def test_round_trip_default(self) -> None:
        original = FanConfig()
        dumped = original.model_dump(mode="python")
        restored = FanConfig.model_validate(dumped)
        assert restored == original

    def test_round_trip_custom_values(self) -> None:
        original = FanConfig(
            mode=FanMode.MANUAL,
            manual_duty=[100, 150, 200],
            thresholds=FanThresholds(
                low_temp=25,
                high_temp=60,
                schmitt=5,
                low_speed=50,
                mid_speed=100,
                high_speed=200,
            ),
            rpi_follow_min=10,
            rpi_follow_max=240,
            is_run_on_startup=False,
        )
        dumped = original.model_dump(mode="python")
        restored = FanConfig.model_validate(dumped)
        assert restored.mode == FanMode.MANUAL
        assert restored.manual_duty == [100, 150, 200]
        assert restored.thresholds.low_temp == 25
        assert restored.rpi_follow_min == 10
        assert restored.is_run_on_startup is False

    def test_fan_config_json_round_trip(self) -> None:
        original = FanConfig(mode=FanMode.OFF)
        json_str = original.model_dump_json()
        restored = FanConfig.model_validate_json(json_str)
        assert restored.mode == FanMode.OFF


# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


class TestLedModeEnum:
    """Verify LedMode enum integer values."""

    def test_rainbow(self) -> None:
        assert LedMode.RAINBOW == 0

    def test_breathing(self) -> None:
        assert LedMode.BREATHING == 1

    def test_follow_temp(self) -> None:
        assert LedMode.FOLLOW_TEMP == 2

    def test_manual(self) -> None:
        assert LedMode.MANUAL == 3

    def test_custom(self) -> None:
        assert LedMode.CUSTOM == 4

    def test_off(self) -> None:
        assert LedMode.OFF == 5

    def test_all_values_sequential(self) -> None:
        values = [m.value for m in LedMode]
        assert values == [0, 1, 2, 3, 4, 5]


class TestFanModeEnum:
    """Verify FanMode enum integer values."""

    def test_follow_temp(self) -> None:
        assert FanMode.FOLLOW_TEMP == 0

    def test_follow_rpi(self) -> None:
        assert FanMode.FOLLOW_RPI == 1

    def test_manual(self) -> None:
        assert FanMode.MANUAL == 2

    def test_custom(self) -> None:
        assert FanMode.CUSTOM == 3

    def test_off(self) -> None:
        assert FanMode.OFF == 4

    def test_all_values_sequential(self) -> None:
        values = [m.value for m in FanMode]
        assert values == [0, 1, 2, 3, 4]

    def test_fan_mode_from_int(self) -> None:
        assert FanMode(2) == FanMode.MANUAL


# ---------------------------------------------------------------------------
# SystemMetrics defaults
# ---------------------------------------------------------------------------


class TestSystemMetricsDefaults:
    """Verify SystemMetrics has safe zero/empty defaults."""

    def test_defaults(self) -> None:
        sm = SystemMetrics()
        assert sm.cpu_percent == 0.0
        assert sm.memory_percent == 0.0
        assert sm.disk_percent == 0.0
        assert sm.cpu_temp == 0.0
        assert sm.case_temp == 0.0
        assert sm.ip_address == ""
        assert sm.fan_duty == [0, 0, 0]
        assert sm.motor_speed == [0, 0, 0]
        assert sm.date == ""
        assert sm.weekday == ""
        assert sm.time == ""
        assert sm.rpi_fan_duty == 0

    def test_all_fields_present(self) -> None:
        """Ensure all documented fields exist in a fresh instance."""
        sm = SystemMetrics()
        fields = set(sm.model_fields.keys())
        expected = {
            "cpu_percent",
            "memory_percent",
            "disk_percent",
            "cpu_temp",
            "case_temp",
            "ip_address",
            "fan_duty",
            "motor_speed",
            "date",
            "weekday",
            "time",
            "rpi_fan_duty",
        }
        assert fields == expected

    def test_system_metrics_accepts_values(self) -> None:
        sm = SystemMetrics(
            cpu_percent=50.0,
            memory_percent=75.0,
            cpu_temp=55.2,
            fan_duty=[100, 150, 200],
        )
        assert sm.cpu_percent == 50.0
        assert sm.fan_duty == [100, 150, 200]


# ---------------------------------------------------------------------------
# OLED configuration
# ---------------------------------------------------------------------------


class TestOledConfig:
    """Verify OLED config defaults, especially the four screens."""

    def test_default_has_four_screens(self) -> None:
        oled = OledConfig()
        assert len(oled.screens) == 4

    def test_all_screens_enabled_by_default(self) -> None:
        oled = OledConfig()
        for screen in oled.screens:
            assert screen.enabled is True

    def test_default_display_time(self) -> None:
        oled = OledConfig()
        for screen in oled.screens:
            assert screen.display_time == 5.0

    def test_default_rotation(self) -> None:
        oled = OledConfig()
        assert oled.rotation == 180

    def test_screen_config_fields(self) -> None:
        sc = OledScreenConfig()
        assert sc.date_format == 0
        assert sc.time_format == 0
        assert sc.interchange == 0

    def test_oled_screens_are_independent_instances(self) -> None:
        """Each screen config should be a distinct object."""
        oled = OledConfig()
        assert oled.screens[0] is not oled.screens[1]


# ---------------------------------------------------------------------------
# Full config round-trip
# ---------------------------------------------------------------------------


class TestFullConfigRoundTrip:
    """Verify full CaseCtlConfig serialization round-trip."""

    def test_full_round_trip(self) -> None:
        original = CaseCtlConfig(
            fan=FanConfig(mode=FanMode.MANUAL),
            led=LedConfig(mode=LedMode.OFF),
            plugins={"custom_plugin": {"enabled": True, "interval": 10}},
        )
        dumped = original.model_dump(mode="python")
        restored = CaseCtlConfig.model_validate(dumped)
        assert restored.fan.mode == FanMode.MANUAL
        assert restored.led.mode == LedMode.OFF
        assert restored.plugins == {"custom_plugin": {"enabled": True, "interval": 10}}
