"""Tests for casectl.config.manager — ConfigManager async YAML config."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from casectl.config.manager import ConfigManager, _deep_merge
from casectl.config.models import CaseCtlConfig, FanConfig, FanMode


# ---------------------------------------------------------------------------
# load: first load creates defaults
# ---------------------------------------------------------------------------


class TestLoadCreatesDefault:
    """Verify that loading a non-existent config creates the file with defaults."""

    async def test_load_creates_default_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "casectl" / "config.yaml"
        mgr = ConfigManager(path=config_file)

        cfg = await mgr.load()

        assert isinstance(cfg, CaseCtlConfig)
        assert config_file.exists()
        # File content should be valid YAML
        content = config_file.read_text(encoding="utf-8")
        assert "fan" in content
        assert "led" in content

    async def test_load_creates_directory_tree(self, tmp_path: Path) -> None:
        config_file = tmp_path / "deep" / "nested" / "dir" / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()
        assert config_file.parent.is_dir()

    async def test_load_returns_default_fan_mode(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        cfg = await mgr.load()
        assert cfg.fan.mode == FanMode.FOLLOW_TEMP

    async def test_load_twice_returns_same_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        cfg1 = await mgr.load()
        cfg2 = await mgr.load()
        assert cfg1.fan.mode == cfg2.fan.mode


# ---------------------------------------------------------------------------
# load: corrupt YAML uses defaults
# ---------------------------------------------------------------------------


class TestLoadCorruptYaml:
    """Verify that corrupt YAML triggers backup + defaults."""

    async def test_corrupt_yaml_uses_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{{{{invalid yaml!@#$%", encoding="utf-8")

        mgr = ConfigManager(path=config_file)
        cfg = await mgr.load()

        assert isinstance(cfg, CaseCtlConfig)
        # The corrupt file should have been backed up
        bak_file = config_file.with_suffix(".yaml.bak")
        assert bak_file.exists()
        assert bak_file.read_text(encoding="utf-8") == "{{{{invalid yaml!@#$%"

    async def test_non_mapping_yaml_uses_defaults(self, tmp_path: Path) -> None:
        """A YAML file that parses as a list (not mapping) -> defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- item1\n- item2\n", encoding="utf-8")

        mgr = ConfigManager(path=config_file)
        cfg = await mgr.load()
        assert isinstance(cfg, CaseCtlConfig)

    async def test_partially_invalid_yaml_recovers(self, tmp_path: Path) -> None:
        """YAML with some valid and some invalid sections recovers."""
        config_file = tmp_path / "config.yaml"
        # Valid fan section, invalid led section (mode is a string, not int)
        config_file.write_text(
            "fan:\n  mode: 2\nled:\n  mode: 'not_a_mode_value'\n",
            encoding="utf-8",
        )
        mgr = ConfigManager(path=config_file)
        cfg = await mgr.load()
        # Should still produce a valid config
        assert isinstance(cfg, CaseCtlConfig)


# ---------------------------------------------------------------------------
# save + reload
# ---------------------------------------------------------------------------


class TestSavePreservesFormat:
    """Verify save then reload produces the same config."""

    async def test_save_reload_round_trip(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)

        # Create a custom config, save, then reload
        custom = CaseCtlConfig(
            fan=FanConfig(mode=FanMode.MANUAL, manual_duty=[100, 150, 200]),
        )
        await mgr.save(custom)

        # Create a fresh manager and reload
        mgr2 = ConfigManager(path=config_file)
        loaded = await mgr2.load()

        assert loaded.fan.mode == FanMode.MANUAL
        assert loaded.fan.manual_duty == [100, 150, 200]

    async def test_save_creates_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "new_config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.save(CaseCtlConfig())
        assert config_file.exists()

    async def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)

        await mgr.save(CaseCtlConfig(fan=FanConfig(mode=FanMode.OFF)))
        await mgr.save(CaseCtlConfig(fan=FanConfig(mode=FanMode.MANUAL)))

        mgr2 = ConfigManager(path=config_file)
        loaded = await mgr2.load()
        assert loaded.fan.mode == FanMode.MANUAL


# ---------------------------------------------------------------------------
# get section
# ---------------------------------------------------------------------------


class TestGetSection:
    """Verify get() returns the correct config section dict."""

    async def test_get_fan_section(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        fan_dict = await mgr.get("fan")
        assert isinstance(fan_dict, dict)
        assert "mode" in fan_dict
        assert fan_dict["mode"] == FanMode.FOLLOW_TEMP

    async def test_get_led_section(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        led_dict = await mgr.get("led")
        assert "mode" in led_dict
        assert "red_value" in led_dict

    async def test_get_unknown_section_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        with pytest.raises(KeyError, match="Unknown config section"):
            await mgr.get("nonexistent")

    async def test_get_auto_loads_if_needed(self, tmp_path: Path) -> None:
        """get() should call load() automatically if config is not cached."""
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        # Do not call load() — get() should auto-load
        fan_dict = await mgr.get("fan")
        assert "mode" in fan_dict

    async def test_get_plugins_section(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        plugins = await mgr.get("plugins")
        assert isinstance(plugins, dict)


# ---------------------------------------------------------------------------
# update section
# ---------------------------------------------------------------------------


class TestUpdateSection:
    """Verify update() merges and persists changes."""

    async def test_update_fan_mode(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        new_cfg = await mgr.update("fan", {"mode": 2})  # MANUAL
        assert new_cfg.fan.mode == FanMode.MANUAL

    async def test_update_persists_to_disk(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        await mgr.update("fan", {"mode": 2})

        # Reload from disk with a fresh manager
        mgr2 = ConfigManager(path=config_file)
        cfg = await mgr2.load()
        assert cfg.fan.mode == FanMode.MANUAL

    async def test_update_preserves_other_sections(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        await mgr.update("fan", {"mode": 4})  # OFF
        cfg = mgr.config
        # LED section should be unchanged
        assert cfg.led.mode == 0  # RAINBOW

    async def test_update_unknown_section_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        with pytest.raises(KeyError, match="Unknown config section"):
            await mgr.update("bogus", {"key": "value"})

    async def test_update_nested_thresholds(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        await mgr.update(
            "fan",
            {"thresholds": {"low_temp": 20, "high_temp": 60}},
        )
        cfg = mgr.config
        assert cfg.fan.thresholds.low_temp == 20
        assert cfg.fan.thresholds.high_temp == 60


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


class TestConfigFilePermissions:
    """Verify config file has 0o600 permissions (owner-only read/write)."""

    async def test_new_config_has_0600_permissions(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.load()

        file_stat = config_file.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        assert mode == 0o600

    async def test_saved_config_has_0600_permissions(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        await mgr.save(CaseCtlConfig())

        file_stat = config_file.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Config property before load
# ---------------------------------------------------------------------------


class TestConfigProperty:
    """Verify config property raises before load."""

    def test_config_property_raises_before_load(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        mgr = ConfigManager(path=config_file)
        with pytest.raises(RuntimeError, match="Config not loaded yet"):
            _ = mgr.config


# ---------------------------------------------------------------------------
# _deep_merge utility
# ---------------------------------------------------------------------------


class TestDeepMerge:
    """Verify the _deep_merge utility function."""

    def test_shallow_merge(self) -> None:
        base: dict[str, Any] = {"a": 1, "b": 2}
        overrides: dict[str, Any] = {"b": 3, "c": 4}
        _deep_merge(base, overrides)
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        base: dict[str, Any] = {"outer": {"a": 1, "b": 2}}
        overrides: dict[str, Any] = {"outer": {"b": 99, "c": 3}}
        _deep_merge(base, overrides)
        assert base == {"outer": {"a": 1, "b": 99, "c": 3}}

    def test_non_dict_overwrite(self) -> None:
        base: dict[str, Any] = {"key": {"nested": True}}
        overrides: dict[str, Any] = {"key": "flat_value"}
        _deep_merge(base, overrides)
        assert base["key"] == "flat_value"
