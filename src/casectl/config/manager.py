"""Async-safe configuration manager for casectl.

Reads and writes ``config.yaml`` using ruamel.yaml (round-trip mode) so that
user comments and formatting are preserved across saves.  All public methods
are coroutine-safe thanks to an internal ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from casectl.config.models import CaseCtlConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_config_path() -> Path:
    """Return the path to ``config.yaml`` respecting ``XDG_CONFIG_HOME``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    return base / "casectl" / "config.yaml"


def _generate_default_yaml(config: CaseCtlConfig) -> str:
    """Render a commented default YAML string from a *CaseCtlConfig*.

    Uses ruamel.yaml round-trip mode so the output is human-friendly.
    """
    import io

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)

    data = config.model_dump(mode="json")  # json mode converts enums to plain ints/strings
    stream = io.StringIO()
    yaml.dump(data, stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Async-safe manager for the casectl YAML configuration file.

    Usage::

        mgr = ConfigManager()
        cfg = await mgr.load()
        fan_dict = await mgr.get("fan")
        cfg = await mgr.update("fan", {"mode": 2, "manual_duty": [100, 100, 100]})

    Parameters:
        path: Override the default config file location (useful for tests).
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path: Path = Path(path) if path else _default_config_path()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._config: CaseCtlConfig | None = None
        self._yaml_rt = YAML()  # round-trip (default typ)
        self._yaml_rt.default_flow_style = False
        self._yaml_rt.indent(mapping=2, sequence=4, offset=2)
        self._yaml_rt.preserve_quotes = True

    # -- public properties ---------------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path to the configuration file."""
        return self._path

    @property
    def config(self) -> CaseCtlConfig:
        """Return the cached config, raising if :meth:`load` hasn't been called.

        For async contexts prefer ``await mgr.load()`` which guarantees the
        cache is populated.
        """
        if self._config is None:
            raise RuntimeError(
                "Config not loaded yet — call `await mgr.load()` first"
            )
        return self._config

    # -- core async API ------------------------------------------------------

    async def load(self) -> CaseCtlConfig:
        """Load (or create) the configuration file and return a validated model.

        * If the file does not exist, a default config is written first.
        * If the YAML is corrupt, it is backed up and defaults are used.
        * If Pydantic validation fails, the offending fields are logged and
          defaults are used for those fields.
        """
        async with self._lock:
            self._ensure_directory()

            if not self._path.exists():
                logger.info("No config found at %s — creating defaults", self._path)
                self._config = CaseCtlConfig()
                self._write_yaml(self._config)
                return self._config

            raw_text = self._path.read_text(encoding="utf-8")

            # --- parse YAML ---------------------------------------------------
            data: dict[str, Any] | None = None
            try:
                yaml_safe = YAML(typ="safe")
                data = yaml_safe.load(raw_text)
            except Exception:
                logger.warning(
                    "Corrupt YAML in %s — backing up and using defaults",
                    self._path,
                    exc_info=True,
                )
                self._backup()
                self._config = CaseCtlConfig()
                self._write_yaml(self._config)
                return self._config

            if not isinstance(data, dict):
                logger.warning(
                    "Config file %s did not contain a YAML mapping — using defaults",
                    self._path,
                )
                self._backup()
                self._config = CaseCtlConfig()
                self._write_yaml(self._config)
                return self._config

            # --- validate via Pydantic ----------------------------------------
            try:
                self._config = CaseCtlConfig.model_validate(data)
            except ValidationError as exc:
                failed_fields = [
                    ".".join(str(loc) for loc in e["loc"]) for e in exc.errors()
                ]
                logger.warning(
                    "Validation errors in %s (fields: %s) — using defaults for invalid fields",
                    self._path,
                    ", ".join(failed_fields),
                )
                # Attempt a lenient rebuild: strip invalid top-level sections
                # and let Pydantic fill them with defaults.
                safe_data: dict[str, Any] = {}
                for section_name in CaseCtlConfig.model_fields:
                    section_value = data.get(section_name)
                    if section_value is not None:
                        try:
                            field_info = CaseCtlConfig.model_fields[section_name]
                            field_type = field_info.annotation
                            if field_type is not None and isinstance(section_value, dict):
                                # Validate the individual section
                                field_type(**section_value)  # type: ignore[operator]
                            safe_data[section_name] = section_value
                        except Exception:
                            logger.debug(
                                "Dropping invalid section '%s' — will use defaults",
                                section_name,
                            )
                try:
                    self._config = CaseCtlConfig.model_validate(safe_data)
                except ValidationError:
                    logger.warning("Could not recover any sections — using full defaults")
                    self._config = CaseCtlConfig()

            return self._config

    async def save(self, config: CaseCtlConfig) -> None:
        """Persist *config* to disk, preserving any existing YAML comments.

        If the file already exists, the round-trip loader is used to read its
        structure first, then values are merged in so comments survive.
        """
        async with self._lock:
            self._ensure_directory()
            self._config = config
            self._write_yaml_roundtrip(config)

    async def get(self, section: str) -> dict[str, Any]:
        """Return a single config section as a plain dict.

        Calls :meth:`load` automatically if the cache is empty.

        Raises:
            KeyError: If *section* is not a valid top-level config key.
        """
        if self._config is None:
            await self.load()
        assert self._config is not None  # for type-checker

        if section not in CaseCtlConfig.model_fields:
            raise KeyError(
                f"Unknown config section '{section}'. "
                f"Valid sections: {', '.join(CaseCtlConfig.model_fields)}"
            )

        section_model = getattr(self._config, section)
        if isinstance(section_model, dict):
            return dict(section_model)
        return section_model.model_dump(mode="python")

    async def update(self, section: str, values: dict[str, Any]) -> CaseCtlConfig:
        """Merge *values* into a config section, validate, and save.

        Returns the full updated :class:`CaseCtlConfig`.

        Raises:
            KeyError: If *section* is not a valid top-level config key.
            pydantic.ValidationError: If the merged values fail validation.
        """
        if self._config is None:
            await self.load()
        assert self._config is not None

        if section not in CaseCtlConfig.model_fields:
            raise KeyError(
                f"Unknown config section '{section}'. "
                f"Valid sections: {', '.join(CaseCtlConfig.model_fields)}"
            )

        # Build a full config dict, merge the target section, and re-validate.
        full = self._config.model_dump(mode="python")
        if isinstance(full[section], dict):
            full[section].update(values)
        else:
            # Should not happen with well-formed models, but handle gracefully.
            full[section] = values

        new_config = CaseCtlConfig.model_validate(full)
        await self.save(new_config)
        return new_config

    # -- private helpers -----------------------------------------------------

    def _ensure_directory(self) -> None:
        """Create the config directory tree if it doesn't exist."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _backup(self, *, max_backups: int = 3) -> None:
        """Copy the current config file to a timestamped ``.bak`` file.

        Backup filenames use the pattern ``config.yaml.<YYYYMMDD_HHMMSS>.bak``.
        Only the most recent *max_backups* backups are kept; older ones are
        deleted automatically.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        bak = self._path.parent / f"{self._path.name}.{stamp}.bak"
        try:
            shutil.copy2(self._path, bak)
            logger.info("Backed up config to %s", bak)
        except OSError:
            logger.error("Failed to create backup at %s", bak, exc_info=True)
            return

        # Prune old backups, keeping only the newest *max_backups*.
        self._prune_backups(max_backups=max_backups)

    def _prune_backups(self, *, max_backups: int = 3) -> None:
        """Remove old timestamped backups exceeding *max_backups*."""
        pattern = f"{self._path.name}.*.bak"
        backups = sorted(
            self._path.parent.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[max_backups:]:
            try:
                old.unlink()
                logger.debug("Removed old backup %s", old)
            except OSError:
                logger.warning("Could not remove old backup %s", old, exc_info=True)

    def _write_yaml(self, config: CaseCtlConfig) -> None:
        """Write config as fresh YAML (no comment preservation)."""
        content = _generate_default_yaml(config)
        self._path.write_text(content, encoding="utf-8")
        self._path.chmod(0o600)  # Restrict access — config may contain secrets
        logger.debug("Wrote config to %s", self._path)

    def _write_yaml_roundtrip(self, config: CaseCtlConfig) -> None:
        """Write config using round-trip mode to preserve existing comments.

        If the file exists, the existing YAML tree is loaded first and values
        are deep-merged so that comments attached to keys survive.
        """
        new_data = config.model_dump(mode="json")

        if self._path.exists():
            try:
                existing_text = self._path.read_text(encoding="utf-8")
                existing = self._yaml_rt.load(existing_text)
                if isinstance(existing, dict):
                    _deep_merge(existing, new_data)
                    merged = existing
                else:
                    merged = new_data
            except Exception:
                logger.debug(
                    "Round-trip load failed — writing fresh YAML",
                    exc_info=True,
                )
                merged = new_data
        else:
            merged = new_data

        with self._path.open("w", encoding="utf-8") as fh:
            self._yaml_rt.dump(merged, fh)
        self._path.chmod(0o600)  # Restrict access — config may contain secrets
        logger.debug("Saved config (round-trip) to %s", self._path)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Recursively merge *overrides* into *base* **in place**.

    Nested dicts are merged; all other values are overwritten.
    """
    for key, value in overrides.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
