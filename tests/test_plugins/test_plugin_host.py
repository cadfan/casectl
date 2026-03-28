"""Tests for casectl.daemon.plugin_host.PluginHost.

Uses mock plugin classes that implement the CasePlugin protocol to exercise
load, setup, start, stop, and query methods without real hardware or I2C.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter

from casectl.daemon.plugin_host import PluginHost, _version_satisfies
from casectl.plugins.base import HardwareRegistry, PluginContext, PluginStatus


# ---------------------------------------------------------------------------
# Mock plugin helpers
# ---------------------------------------------------------------------------


class _MockPlugin:
    """Minimal CasePlugin-compatible mock that records lifecycle calls."""

    name: str = "mock-plugin"
    version: str = "1.0.0"
    description: str = "A mock plugin for testing"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self.setup_called = False
        self.start_called = False
        self.stop_called = False
        self._router = APIRouter()

    async def setup(self, ctx: PluginContext) -> None:
        self.setup_called = True
        ctx.register_routes(self._router)

    async def start(self) -> None:
        self.start_called = True

    async def stop(self) -> None:
        self.stop_called = True

    def get_status(self) -> dict[str, Any]:
        return {"status": PluginStatus.HEALTHY}


class _MockPluginNoRoutes:
    """Plugin that does not register any routes."""

    name: str = "no-routes"
    version: str = "0.5.0"
    description: str = "Plugin without routes"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        self.setup_called = False
        self.start_called = False
        self.stop_called = False

    async def setup(self, ctx: PluginContext) -> None:
        self.setup_called = True

    async def start(self) -> None:
        self.start_called = True

    async def stop(self) -> None:
        self.stop_called = True

    def get_status(self) -> dict[str, Any]:
        return {"status": PluginStatus.HEALTHY}


class _HighVersionPlugin:
    """Plugin that requires a daemon version higher than what we'll provide."""

    name: str = "future-plugin"
    version: str = "2.0.0"
    description: str = "Requires a future daemon"
    min_daemon_version: str = "99.0.0"

    def __init__(self) -> None:
        pass

    async def setup(self, ctx: PluginContext) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def get_status(self) -> dict[str, Any]:
        return {"status": PluginStatus.STOPPED}


class _NonConformingPlugin:
    """Plugin that does NOT satisfy the CasePlugin protocol (missing required attrs)."""

    def __init__(self) -> None:
        pass
    # Missing: name, version, description, min_daemon_version, setup, start, stop, get_status


class _ExplodingSetupPlugin:
    """Plugin whose setup() raises an exception."""

    name: str = "exploding"
    version: str = "0.1.0"
    description: str = "Blows up in setup"
    min_daemon_version: str = "0.1.0"

    def __init__(self) -> None:
        pass

    async def setup(self, ctx: PluginContext) -> None:
        raise RuntimeError("setup exploded")

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def get_status(self) -> dict[str, Any]:
        return {"status": PluginStatus.ERROR}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def host() -> PluginHost:
    """Return a PluginHost with mocked dependencies."""
    config_mgr = AsyncMock()
    hw_registry = HardwareRegistry(expansion=None, oled=None, system_info=None)
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    return PluginHost(
        config_manager=config_mgr,
        hardware_registry=hw_registry,
        event_bus=event_bus,
        daemon_version="0.1.0",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_load_single_plugin(host: PluginHost) -> None:
    """Loading a compatible mock plugin succeeds and is accessible via get_plugin."""
    await host._load_single_plugin(_MockPlugin)

    plugin = host.get_plugin("mock-plugin")
    assert plugin is not None
    assert plugin.setup_called is True


async def test_incompatible_version_skipped(host: PluginHost) -> None:
    """A plugin requiring a higher daemon version is not loaded."""
    await host._load_single_plugin(_HighVersionPlugin)

    assert host.get_plugin("future-plugin") is None


async def test_setup_error_marks_plugin_error(host: PluginHost) -> None:
    """Plugin that raises in setup() is stored with ERROR status."""
    await host._load_single_plugin(_ExplodingSetupPlugin)

    plugin = host.get_plugin("exploding")
    assert plugin is not None
    assert host._plugin_statuses["exploding"] == PluginStatus.ERROR


async def test_setup_error_does_not_block_others(host: PluginHost) -> None:
    """An erroring plugin does not prevent subsequent plugins from loading."""
    await host._load_single_plugin(_ExplodingSetupPlugin)
    await host._load_single_plugin(_MockPluginNoRoutes)

    assert host.get_plugin("exploding") is not None
    assert host.get_plugin("no-routes") is not None
    assert host._plugin_statuses["exploding"] == PluginStatus.ERROR
    assert host._plugin_statuses["no-routes"] == PluginStatus.STOPPED


async def test_duplicate_name_rejected(host: PluginHost) -> None:
    """Loading a plugin with the same name twice keeps the first instance."""
    await host._load_single_plugin(_MockPlugin)
    first = host.get_plugin("mock-plugin")

    # Try to load again
    await host._load_single_plugin(_MockPlugin)

    # Still the same instance
    assert host.get_plugin("mock-plugin") is first


async def test_duplicate_route_prefix_rejected(host: PluginHost) -> None:
    """A second plugin claiming the same route prefix is rejected."""
    # Load a plugin with routes
    await host._load_single_plugin(_MockPlugin)

    # Manually inject a duplicate route prefix to trigger collision check
    # The prefix for mock-plugin is /api/plugins/mock-plugin
    # Create a plugin class with the same name but different class
    class _DuplicateRoutePlugin:
        name = "mock-plugin-dup"
        version = "0.1.0"
        description = "Dup"
        min_daemon_version = "0.1.0"

        def __init__(self):
            self._router = APIRouter()

        async def setup(self, ctx):
            ctx.register_routes(self._router)

        async def start(self):
            pass

        async def stop(self):
            pass

        def get_status(self):
            return {"status": PluginStatus.HEALTHY}

    # Manually insert an existing route prefix to force collision
    host._routes["/api/plugins/mock-plugin-dup"] = APIRouter()

    await host._load_single_plugin(_DuplicateRoutePlugin)

    # The plugin should not have been stored because its prefix is already taken
    assert host.get_plugin("mock-plugin-dup") is None


async def test_start_all_calls_start(host: PluginHost) -> None:
    """start_all() calls start() on each loaded plugin."""
    await host._load_single_plugin(_MockPlugin)
    await host._load_single_plugin(_MockPluginNoRoutes)

    await host.start_all()

    p1 = host.get_plugin("mock-plugin")
    p2 = host.get_plugin("no-routes")
    assert p1.start_called is True
    assert p2.start_called is True

    await host.stop_all()  # clean up watchdog


async def test_start_all_skips_errored(host: PluginHost) -> None:
    """start_all() skips plugins in ERROR status."""
    await host._load_single_plugin(_ExplodingSetupPlugin)

    await host.start_all()

    # The exploding plugin should still be ERROR, not HEALTHY
    assert host._plugin_statuses["exploding"] == PluginStatus.ERROR

    await host.stop_all()  # clean up watchdog


async def test_stop_all_reverse_order(host: PluginHost) -> None:
    """stop_all() calls stop() on each plugin in reverse load order."""
    await host._load_single_plugin(_MockPlugin)
    await host._load_single_plugin(_MockPluginNoRoutes)
    await host.start_all()

    stop_order: list[str] = []
    orig_stop_mock = host.get_plugin("mock-plugin").stop
    orig_stop_no = host.get_plugin("no-routes").stop

    async def track_mock():
        stop_order.append("mock-plugin")
        await orig_stop_mock()

    async def track_no():
        stop_order.append("no-routes")
        await orig_stop_no()

    host.get_plugin("mock-plugin").stop = track_mock
    host.get_plugin("no-routes").stop = track_no

    await host.stop_all()

    # Reverse order: no-routes was loaded second, so stopped first
    assert stop_order == ["no-routes", "mock-plugin"]


async def test_get_plugin_returns_none_for_missing(host: PluginHost) -> None:
    """get_plugin() returns None when the plugin does not exist."""
    assert host.get_plugin("nonexistent") is None


async def test_list_plugins_format(host: PluginHost) -> None:
    """list_plugins() returns dicts with name, version, status, description."""
    await host._load_single_plugin(_MockPlugin)
    await host._load_single_plugin(_MockPluginNoRoutes)

    result = host.list_plugins()
    assert len(result) == 2

    names = {p["name"] for p in result}
    assert "mock-plugin" in names
    assert "no-routes" in names

    for entry in result:
        assert "name" in entry
        assert "version" in entry
        assert "status" in entry
        assert "description" in entry


# ---------------------------------------------------------------------------
# Plugin whitelist tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def host_with_whitelist() -> PluginHost:
    """Return a PluginHost with allowed_plugins set to only allow 'trusted-plugin'."""
    config_mgr = AsyncMock()
    hw_registry = HardwareRegistry(expansion=None, oled=None, system_info=None)
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    return PluginHost(
        config_manager=config_mgr,
        hardware_registry=hw_registry,
        event_bus=event_bus,
        daemon_version="0.1.0",
        allowed_plugins=["trusted-plugin"],
    )


@pytest.fixture()
def host_with_empty_whitelist() -> PluginHost:
    """Return a PluginHost with an empty allowed_plugins (blocks all community)."""
    config_mgr = AsyncMock()
    hw_registry = HardwareRegistry(expansion=None, oled=None, system_info=None)
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    return PluginHost(
        config_manager=config_mgr,
        hardware_registry=hw_registry,
        event_bus=event_bus,
        daemon_version="0.1.0",
        allowed_plugins=[],
    )


def test_allowed_plugins_none_by_default(host: PluginHost) -> None:
    """Default PluginHost has no whitelist (None = allow all)."""
    assert host._allowed_plugins is None


def test_allowed_plugins_stored(host_with_whitelist: PluginHost) -> None:
    """PluginHost stores the allowed_plugins list."""
    assert host_with_whitelist._allowed_plugins == ["trusted-plugin"]


async def test_whitelist_does_not_affect_builtin_loading(
    host_with_empty_whitelist: PluginHost,
) -> None:
    """Built-in plugins load even when allowed_plugins is an empty list.

    The whitelist only affects community plugins discovered via entry points,
    not built-in plugins loaded by module path.
    """
    # Built-in plugins are loaded via _load_single_plugin, not affected by whitelist
    await host_with_empty_whitelist._load_single_plugin(_MockPlugin)
    assert host_with_empty_whitelist.get_plugin("mock-plugin") is not None


def _make_mock_entry_points(*ep_specs: tuple[str, str]):
    """Create a mock entry_points function returning mock entry points.

    Each *ep_specs* is (name, value).  The returned function replaces
    ``importlib.metadata.entry_points`` in the plugin_host module.
    """
    eps = []
    for name, value in ep_specs:
        ep = MagicMock()
        ep.name = name
        ep.value = value
        ep.load.return_value = type(name, (), {})  # unique class per ep
        eps.append(ep)

    def _mock_entry_points(group: str = ""):
        return eps

    return _mock_entry_points, eps


async def test_whitelist_blocks_unlisted_community_plugins(
    host_with_whitelist: PluginHost,
) -> None:
    """Community plugins not in the whitelist are blocked during discovery."""
    mock_ep_fn, eps = _make_mock_entry_points(
        ("trusted-plugin", "some.module:TrustedPlugin"),
        ("malicious-plugin", "evil.module:BadPlugin"),
    )
    trusted_ep, blocked_ep = eps

    # Patch only the entry_points import inside the method
    with patch.object(
        host_with_whitelist, "_discover_plugin_classes",
        wraps=host_with_whitelist._discover_plugin_classes,
    ):
        # We need to patch the entry_points call inside the method
        import importlib.metadata as _im
        original_ep = _im.entry_points
        _im.entry_points = mock_ep_fn

        # Also suppress builtin loading to isolate community plugin behavior
        original_builtins = __import__("casectl.daemon.plugin_host", fromlist=["_BUILTIN_PLUGINS"])
        saved = original_builtins._BUILTIN_PLUGINS[:]
        original_builtins._BUILTIN_PLUGINS = []
        try:
            classes = host_with_whitelist._discover_plugin_classes()
        finally:
            _im.entry_points = original_ep
            original_builtins._BUILTIN_PLUGINS = saved

    # Only the trusted plugin class should be discovered
    assert trusted_ep.load.called
    blocked_ep.load.assert_not_called()
    assert len(classes) == 1


async def test_whitelist_none_allows_all_community_plugins(
    host: PluginHost,
) -> None:
    """When allowed_plugins is None, all community plugins are permitted."""
    mock_ep_fn, eps = _make_mock_entry_points(
        ("plugin-a", "mod_a:PluginA"),
        ("plugin-b", "mod_b:PluginB"),
    )

    import importlib.metadata as _im
    import casectl.daemon.plugin_host as _ph

    original_ep = _im.entry_points
    saved = _ph._BUILTIN_PLUGINS[:]
    _ph._BUILTIN_PLUGINS = []
    _im.entry_points = mock_ep_fn
    try:
        classes = host._discover_plugin_classes()
    finally:
        _im.entry_points = original_ep
        _ph._BUILTIN_PLUGINS = saved

    assert len(classes) == 2
    for ep in eps:
        assert ep.load.called


async def test_empty_whitelist_blocks_all_community_plugins(
    host_with_empty_whitelist: PluginHost,
) -> None:
    """An empty allowed_plugins list blocks all community plugins."""
    mock_ep_fn, eps = _make_mock_entry_points(
        ("any-plugin", "mod:AnyPlugin"),
    )

    import importlib.metadata as _im
    import casectl.daemon.plugin_host as _ph

    original_ep = _im.entry_points
    saved = _ph._BUILTIN_PLUGINS[:]
    _ph._BUILTIN_PLUGINS = []
    _im.entry_points = mock_ep_fn
    try:
        classes = host_with_empty_whitelist._discover_plugin_classes()
    finally:
        _im.entry_points = original_ep
        _ph._BUILTIN_PLUGINS = saved

    eps[0].load.assert_not_called()
    assert len(classes) == 0


async def test_non_conforming_plugin_rejected(host: PluginHost) -> None:
    """A plugin that doesn't satisfy the CasePlugin protocol is rejected."""
    await host._load_single_plugin(_NonConformingPlugin)

    # The non-conforming plugin should not be loaded
    assert host.get_plugin("_NonConformingPlugin") is None
    assert len(host._plugins) == 0


async def test_version_satisfies_basic() -> None:
    """_version_satisfies returns correct results for various version pairs."""
    assert _version_satisfies("0.1.0", "0.1.0") is True
    assert _version_satisfies("1.0.0", "0.1.0") is True
    assert _version_satisfies("0.1.0", "1.0.0") is False
    assert _version_satisfies("0.2.0", "0.1.0") is True
