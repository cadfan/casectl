"""Tests for the automation rules engine core."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from casectl.plugins.automation.engine import (
    ActionRegistry,
    ConflictRecord,
    EngineStats,
    RulesEngine,
    _resolve_field,
    evaluate_condition,
    evaluate_conditions,
)
from casectl.plugins.automation.models import (
    AutomationConfig,
    AutomationRule,
    ConditionOperator,
    PRIORITY_ORDER,
    RuleAction,
    RuleCondition,
    RulePriority,
)


# ---------------------------------------------------------------------------
# _resolve_field
# ---------------------------------------------------------------------------


class TestResolveField:
    """Tests for dot-path field resolution."""

    def test_simple_dict_key(self) -> None:
        assert _resolve_field({"cpu_temp": 75.0}, "cpu_temp") == 75.0

    def test_nested_dict_key(self) -> None:
        data = {"fan": {"duty": [100, 100, 100]}}
        assert _resolve_field(data, "fan.duty") == [100, 100, 100]

    def test_deeply_nested(self) -> None:
        data = {"a": {"b": {"c": 42}}}
        assert _resolve_field(data, "a.b.c") == 42

    def test_missing_key_raises(self) -> None:
        with pytest.raises(KeyError):
            _resolve_field({"x": 1}, "y")

    def test_missing_nested_key_raises(self) -> None:
        with pytest.raises(KeyError):
            _resolve_field({"a": {"b": 1}}, "a.c")

    def test_attribute_access(self) -> None:
        class Obj:
            x = 42
        assert _resolve_field(Obj(), "x") == 42

    def test_non_dict_non_attr_raises(self) -> None:
        with pytest.raises(KeyError):
            _resolve_field(42, "value")


# ---------------------------------------------------------------------------
# evaluate_condition
# ---------------------------------------------------------------------------


class TestEvaluateCondition:
    """Tests for individual condition evaluation."""

    def _cond(self, field: str, op: str, value: Any) -> RuleCondition:
        return RuleCondition(field=field, operator=ConditionOperator(op), value=value)

    def test_gt_true(self) -> None:
        assert evaluate_condition(self._cond("temp", "gt", 50), {"temp": 75}) is True

    def test_gt_false(self) -> None:
        assert evaluate_condition(self._cond("temp", "gt", 50), {"temp": 30}) is False

    def test_gt_equal_false(self) -> None:
        assert evaluate_condition(self._cond("temp", "gt", 50), {"temp": 50}) is False

    def test_gte_true(self) -> None:
        assert evaluate_condition(self._cond("temp", "gte", 50), {"temp": 50}) is True

    def test_lt_true(self) -> None:
        assert evaluate_condition(self._cond("temp", "lt", 50), {"temp": 30}) is True

    def test_lt_false(self) -> None:
        assert evaluate_condition(self._cond("temp", "lt", 50), {"temp": 75}) is False

    def test_lte_true(self) -> None:
        assert evaluate_condition(self._cond("temp", "lte", 50), {"temp": 50}) is True

    def test_eq_true(self) -> None:
        assert evaluate_condition(self._cond("mode", "eq", "manual"), {"mode": "manual"}) is True

    def test_eq_false(self) -> None:
        assert evaluate_condition(self._cond("mode", "eq", "manual"), {"mode": "auto"}) is False

    def test_neq_true(self) -> None:
        assert evaluate_condition(self._cond("mode", "neq", "off"), {"mode": "auto"}) is True

    def test_neq_false(self) -> None:
        assert evaluate_condition(self._cond("mode", "neq", "off"), {"mode": "off"}) is False

    def test_in_true(self) -> None:
        assert evaluate_condition(
            self._cond("status", "in", ["ok", "warning"]), {"status": "ok"}
        ) is True

    def test_in_false(self) -> None:
        assert evaluate_condition(
            self._cond("status", "in", ["ok", "warning"]), {"status": "error"}
        ) is False

    def test_in_non_list_value(self) -> None:
        """'in' operator with non-list reference returns False."""
        assert evaluate_condition(self._cond("x", "in", 42), {"x": 42}) is False

    def test_not_in_true(self) -> None:
        assert evaluate_condition(
            self._cond("status", "not_in", ["error"]), {"status": "ok"}
        ) is True

    def test_not_in_false(self) -> None:
        assert evaluate_condition(
            self._cond("status", "not_in", ["error"]), {"status": "error"}
        ) is False

    def test_not_in_non_list_value(self) -> None:
        assert evaluate_condition(self._cond("x", "not_in", 42), {"x": 1}) is False

    def test_between_true(self) -> None:
        assert evaluate_condition(
            self._cond("temp", "between", [30, 60]), {"temp": 45}
        ) is True

    def test_between_at_lower_bound(self) -> None:
        assert evaluate_condition(
            self._cond("temp", "between", [30, 60]), {"temp": 30}
        ) is True

    def test_between_at_upper_bound(self) -> None:
        assert evaluate_condition(
            self._cond("temp", "between", [30, 60]), {"temp": 60}
        ) is True

    def test_between_false(self) -> None:
        assert evaluate_condition(
            self._cond("temp", "between", [30, 60]), {"temp": 70}
        ) is False

    def test_between_invalid_list(self) -> None:
        assert evaluate_condition(
            self._cond("temp", "between", [30]), {"temp": 45}
        ) is False

    def test_missing_field_returns_false(self) -> None:
        assert evaluate_condition(self._cond("missing", "gt", 0), {"temp": 50}) is False

    def test_type_mismatch_returns_false(self) -> None:
        """Non-numeric comparison with numeric operator returns False."""
        assert evaluate_condition(
            self._cond("name", "gt", 50), {"name": "hello"}
        ) is False

    def test_nested_field(self) -> None:
        data = {"fan": {"duty": 200}}
        assert evaluate_condition(self._cond("fan.duty", "gte", 150), data) is True

    def test_none_data_returns_false(self) -> None:
        assert evaluate_condition(self._cond("x", "eq", 1), None) is False


# ---------------------------------------------------------------------------
# evaluate_conditions
# ---------------------------------------------------------------------------


class TestEvaluateConditions:
    """Tests for AND-logic multi-condition evaluation."""

    def test_empty_conditions_true(self) -> None:
        """No conditions = unconditional rule."""
        assert evaluate_conditions([], {"anything": True}) is True

    def test_all_true(self) -> None:
        conds = [
            RuleCondition(field="temp", operator=ConditionOperator.GT, value=50),
            RuleCondition(field="mode", operator=ConditionOperator.EQ, value="auto"),
        ]
        assert evaluate_conditions(conds, {"temp": 75, "mode": "auto"}) is True

    def test_one_false(self) -> None:
        conds = [
            RuleCondition(field="temp", operator=ConditionOperator.GT, value=50),
            RuleCondition(field="mode", operator=ConditionOperator.EQ, value="auto"),
        ]
        assert evaluate_conditions(conds, {"temp": 75, "mode": "manual"}) is False

    def test_all_false(self) -> None:
        conds = [
            RuleCondition(field="temp", operator=ConditionOperator.GT, value=50),
            RuleCondition(field="mode", operator=ConditionOperator.EQ, value="auto"),
        ]
        assert evaluate_conditions(conds, {"temp": 30, "mode": "manual"}) is False


# ---------------------------------------------------------------------------
# ActionRegistry
# ---------------------------------------------------------------------------


class TestActionRegistry:
    """Tests for the action handler registry."""

    def test_register_and_get(self) -> None:
        reg = ActionRegistry()
        handler = AsyncMock()
        reg.register("fan", handler)
        assert reg.get("fan") is handler

    def test_get_missing_returns_none(self) -> None:
        reg = ActionRegistry()
        assert reg.get("nonexistent") is None

    def test_unregister(self) -> None:
        reg = ActionRegistry()
        handler = AsyncMock()
        reg.register("fan", handler)
        reg.unregister("fan")
        assert reg.get("fan") is None

    def test_unregister_nonexistent(self) -> None:
        """Unregistering a target that doesn't exist should not raise."""
        reg = ActionRegistry()
        reg.unregister("nonexistent")  # should not raise

    def test_targets_property(self) -> None:
        reg = ActionRegistry()
        reg.register("fan", AsyncMock())
        reg.register("led", AsyncMock())
        assert sorted(reg.targets) == ["fan", "led"]

    def test_overwrite_handler(self) -> None:
        reg = ActionRegistry()
        h1 = AsyncMock()
        h2 = AsyncMock()
        reg.register("fan", h1)
        reg.register("fan", h2)
        assert reg.get("fan") is h2


# ---------------------------------------------------------------------------
# EngineStats
# ---------------------------------------------------------------------------


class TestConflictRecord:
    """Tests for ConflictRecord."""

    def test_creation(self) -> None:
        record = ConflictRecord("fan:set_duty", "safety-rule", 100)
        assert record.conflict_key == "fan:set_duty"
        assert record.winner_rule == "safety-rule"
        assert record.winner_priority == 100
        assert record.suppressed == []

    def test_to_dict_empty_suppressed(self) -> None:
        record = ConflictRecord("fan:set_duty", "safety-rule", 100)
        d = record.to_dict()
        assert d == {
            "conflict_key": "fan:set_duty",
            "winner_rule": "safety-rule",
            "winner_priority": 100,
            "suppressed": [],
        }

    def test_to_dict_with_suppressed(self) -> None:
        record = ConflictRecord("fan:set_duty", "safety-rule", 100)
        record.suppressed.append(("user-rule", 10))
        record.suppressed.append(("scheduled-rule", 50))
        d = record.to_dict()
        assert len(d["suppressed"]) == 2
        assert d["suppressed"][0] == {"rule": "user-rule", "priority": 10}
        assert d["suppressed"][1] == {"rule": "scheduled-rule", "priority": 50}


class TestEngineStats:
    """Tests for EngineStats."""

    def test_initial_values(self) -> None:
        stats = EngineStats()
        d = stats.to_dict()
        assert d["events_processed"] == 0
        assert d["actions_executed"] == 0
        assert d["conflicts_resolved"] == 0
        assert d["last_latency_ms"] == 0.0
        assert d["last_conflicts"] == []

    def test_to_dict_format(self) -> None:
        stats = EngineStats()
        stats.events_processed = 10
        stats.last_latency_ms = 1.2345
        d = stats.to_dict()
        assert d["events_processed"] == 10
        assert d["last_latency_ms"] == 1.23  # rounded to 2 decimal places

    def test_to_dict_includes_conflicts(self) -> None:
        stats = EngineStats()
        stats.conflicts_resolved = 3
        record = ConflictRecord("fan:set_duty", "safety", 100)
        record.suppressed.append(("user", 10))
        stats.last_conflicts = [record]
        d = stats.to_dict()
        assert d["conflicts_resolved"] == 3
        assert len(d["last_conflicts"]) == 1


# ---------------------------------------------------------------------------
# RulesEngine
# ---------------------------------------------------------------------------


def _make_config(rules: list[dict], enabled: bool = True) -> AutomationConfig:
    """Create an AutomationConfig from dicts."""
    return AutomationConfig(enabled=enabled, rules=rules)


def _simple_rule(
    name: str = "test",
    event: str = "metrics_updated",
    priority: str = "user",
    conditions: list | None = None,
    actions: list | None = None,
    cooldown: float = 0.0,
) -> dict:
    """Create a minimal rule dict."""
    return {
        "name": name,
        "event": event,
        "priority": priority,
        "conditions": conditions or [],
        "actions": actions or [{"target": "log", "command": "info", "params": {"message": "test"}}],
        "cooldown": cooldown,
    }


class TestRulesEngine:
    """Tests for the core rules engine."""

    @pytest.fixture()
    def registry(self) -> ActionRegistry:
        reg = ActionRegistry()
        reg.register("fan", AsyncMock())
        reg.register("led", AsyncMock())
        reg.register("log", AsyncMock())
        reg.register("emit", AsyncMock())
        return reg

    async def test_disabled_engine_noop(self, registry: ActionRegistry) -> None:
        config = _make_config(
            [_simple_rule()],
            enabled=False,
        )
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {"cpu_temp": 90})
        assert fired == []
        assert engine.stats.events_processed == 0

    async def test_no_matching_rules(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule(event="other_event")])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})
        assert fired == []

    async def test_unconditional_rule_fires(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule()])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})
        assert fired == ["test"]
        assert engine.stats.events_processed == 1
        assert engine.stats.actions_executed == 1

    async def test_conditions_met_fires(self, registry: ActionRegistry) -> None:
        config = _make_config([
            _simple_rule(
                conditions=[{"field": "cpu_temp", "operator": "gt", "value": 80}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {"cpu_temp": 85})
        assert fired == ["test"]

    async def test_conditions_not_met_skipped(self, registry: ActionRegistry) -> None:
        config = _make_config([
            _simple_rule(
                conditions=[{"field": "cpu_temp", "operator": "gt", "value": 80}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {"cpu_temp": 50})
        assert fired == []
        assert engine.stats.conditions_failed == 1

    async def test_disabled_rule_skipped(self, registry: ActionRegistry) -> None:
        rule = _simple_rule()
        rule["enabled"] = False
        config = _make_config([rule])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})
        assert fired == []

    async def test_cooldown_prevents_rapid_firing(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule(cooldown=60.0)])
        engine = RulesEngine(config, registry)

        # First firing should succeed
        fired1 = await engine.process_event("metrics_updated", {})
        assert fired1 == ["test"]

        # Immediate second firing should be blocked by cooldown
        fired2 = await engine.process_event("metrics_updated", {})
        assert fired2 == []
        assert engine.stats.skipped_cooldown == 1

    async def test_cooldown_zero_allows_rapid_firing(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule(cooldown=0.0)])
        engine = RulesEngine(config, registry)

        fired1 = await engine.process_event("metrics_updated", {})
        fired2 = await engine.process_event("metrics_updated", {})
        assert fired1 == ["test"]
        assert fired2 == ["test"]

    async def test_priority_conflict_resolution(self, registry: ActionRegistry) -> None:
        """Higher-priority rule wins when targeting the same action."""
        config = _make_config([
            _simple_rule(
                name="user-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 100}}],
            ),
            _simple_rule(
                name="safety-fan",
                priority="safety",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 255}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})

        # Safety rule should win
        assert "safety-fan" in fired
        # User rule should NOT fire (same target+command, lower priority)
        assert "user-fan" not in fired

        # The fan handler should have been called with the safety params
        fan_handler = registry.get("fan")
        assert fan_handler is not None
        fan_handler.assert_called_once()
        call_action = fan_handler.call_args[0][0]
        assert call_action.params["duty"] == 255

    async def test_conflict_resolution_tracks_stats(self, registry: ActionRegistry) -> None:
        """Conflicts should be counted in engine stats."""
        config = _make_config([
            _simple_rule(
                name="user-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 100}}],
            ),
            _simple_rule(
                name="safety-fan",
                priority="safety",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 255}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})
        assert engine.stats.conflicts_resolved == 1

    async def test_conflict_records_suppressed_rules(self, registry: ActionRegistry) -> None:
        """Conflict records should list suppressed rules."""
        config = _make_config([
            _simple_rule(
                name="user-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 100}}],
            ),
            _simple_rule(
                name="safety-fan",
                priority="safety",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 255}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})

        conflicts = engine.stats.last_conflicts
        assert len(conflicts) == 1
        record = conflicts[0]
        assert record.conflict_key == "fan:set_duty"
        assert record.winner_rule == "safety-fan"
        assert record.winner_priority == PRIORITY_ORDER[RulePriority.SAFETY]
        assert len(record.suppressed) == 1
        assert record.suppressed[0][0] == "user-fan"

    async def test_same_priority_deterministic_tiebreak_by_name(
        self, registry: ActionRegistry
    ) -> None:
        """Same-priority rules targeting the same action: alphabetically first name wins."""
        config = _make_config([
            _simple_rule(
                name="beta-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 50}}],
            ),
            _simple_rule(
                name="alpha-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 200}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})

        # alpha-fan comes first alphabetically at same priority
        assert "alpha-fan" in fired
        assert "beta-fan" not in fired

        fan_handler = registry.get("fan")
        assert fan_handler is not None
        fan_handler.assert_called_once()
        call_action = fan_handler.call_args[0][0]
        assert call_action.params["duty"] == 200

    async def test_three_way_priority_conflict(self, registry: ActionRegistry) -> None:
        """Safety > scheduled > user when all three target the same action."""
        config = _make_config([
            _simple_rule(
                name="user-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 50}}],
            ),
            _simple_rule(
                name="scheduled-fan",
                priority="scheduled",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 150}}],
            ),
            _simple_rule(
                name="safety-fan",
                priority="safety",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 255}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})

        assert fired == ["safety-fan"]
        assert engine.stats.conflicts_resolved == 2

        # Should record both suppressed rules
        conflicts = engine.stats.last_conflicts
        assert len(conflicts) == 1
        record = conflicts[0]
        assert record.winner_rule == "safety-fan"
        suppressed_names = [s[0] for s in record.suppressed]
        assert "scheduled-fan" in suppressed_names
        assert "user-fan" in suppressed_names

    async def test_different_commands_same_target_no_conflict(
        self, registry: ActionRegistry
    ) -> None:
        """Different commands on the same target are independent (no conflict)."""
        config = _make_config([
            _simple_rule(
                name="fan-duty",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 100}}],
            ),
            _simple_rule(
                name="fan-mode",
                priority="user",
                actions=[{"target": "fan", "command": "set_mode", "params": {"mode": "auto"}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})

        assert sorted(fired) == ["fan-duty", "fan-mode"]
        assert engine.stats.conflicts_resolved == 0

    async def test_mixed_conflict_and_non_conflict_actions(
        self, registry: ActionRegistry
    ) -> None:
        """A rule with multiple actions: some conflict, some don't."""
        config = _make_config([
            _simple_rule(
                name="safety-rule",
                priority="safety",
                actions=[
                    {"target": "fan", "command": "set_duty", "params": {"duty": 255}},
                    {"target": "log", "command": "warn", "params": {"message": "overheat"}},
                ],
            ),
            _simple_rule(
                name="user-rule",
                priority="user",
                actions=[
                    {"target": "fan", "command": "set_duty", "params": {"duty": 100}},
                    {"target": "led", "command": "set_color", "params": {"color": "blue"}},
                ],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})

        # safety-rule wins fan:set_duty, user-rule's led:set_color still fires
        assert "safety-rule" in fired
        assert "user-rule" in fired
        # fan handler gets safety params
        fan_handler = registry.get("fan")
        assert fan_handler is not None
        fan_handler.assert_called_once()
        assert fan_handler.call_args[0][0].params["duty"] == 255
        # led handler also called
        led_handler = registry.get("led")
        assert led_handler is not None
        led_handler.assert_called_once()

    async def test_conflict_records_serialise(self, registry: ActionRegistry) -> None:
        """ConflictRecord.to_dict() should produce the expected structure."""
        config = _make_config([
            _simple_rule(
                name="user-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 100}}],
            ),
            _simple_rule(
                name="safety-fan",
                priority="safety",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 255}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})

        stats_dict = engine.stats.to_dict()
        assert "conflicts_resolved" in stats_dict
        assert stats_dict["conflicts_resolved"] == 1
        assert len(stats_dict["last_conflicts"]) == 1
        conflict = stats_dict["last_conflicts"][0]
        assert conflict["conflict_key"] == "fan:set_duty"
        assert conflict["winner_rule"] == "safety-fan"
        assert len(conflict["suppressed"]) == 1
        assert conflict["suppressed"][0]["rule"] == "user-fan"

    async def test_no_conflict_no_records(self, registry: ActionRegistry) -> None:
        """When there are no conflicts, last_conflicts should be empty."""
        config = _make_config([
            _simple_rule(
                name="fan-rule",
                actions=[{"target": "fan", "command": "set_duty"}],
            ),
        ])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})
        assert engine.stats.last_conflicts == []
        assert engine.stats.conflicts_resolved == 0

    async def test_conflict_resolution_only_for_matching_conditions(
        self, registry: ActionRegistry
    ) -> None:
        """A higher-priority rule that fails conditions should not suppress lower-priority."""
        config = _make_config([
            _simple_rule(
                name="safety-fan",
                priority="safety",
                conditions=[{"field": "cpu_temp", "operator": "gt", "value": 90}],
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 255}}],
            ),
            _simple_rule(
                name="user-fan",
                priority="user",
                actions=[{"target": "fan", "command": "set_duty", "params": {"duty": 100}}],
            ),
        ])
        engine = RulesEngine(config, registry)
        # cpu_temp=50 — safety rule conditions fail, user rule fires
        fired = await engine.process_event("metrics_updated", {"cpu_temp": 50})

        assert fired == ["user-fan"]
        assert engine.stats.conflicts_resolved == 0

    async def test_different_targets_both_fire(self, registry: ActionRegistry) -> None:
        """Rules targeting different subsystems should both fire."""
        config = _make_config([
            _simple_rule(
                name="fan-rule",
                actions=[{"target": "fan", "command": "set_duty"}],
            ),
            _simple_rule(
                name="led-rule",
                actions=[{"target": "led", "command": "set_mode"}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})
        assert sorted(fired) == ["fan-rule", "led-rule"]

    async def test_missing_handler_logged(self, registry: ActionRegistry) -> None:
        config = _make_config([
            _simple_rule(
                actions=[{"target": "nonexistent", "command": "do_thing"}],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})
        assert fired == []
        assert engine.stats.handler_missing == 1

    async def test_action_handler_exception(self, registry: ActionRegistry) -> None:
        """Handler exceptions are caught and logged, not propagated."""
        failing_handler = AsyncMock(side_effect=RuntimeError("boom"))
        registry.register("fan", failing_handler)

        config = _make_config([
            _simple_rule(actions=[{"target": "fan", "command": "set_duty"}]),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})
        # Rule fires but action fails
        assert fired == []
        assert engine.stats.actions_failed == 1

    async def test_multiple_actions_per_rule(self, registry: ActionRegistry) -> None:
        config = _make_config([
            _simple_rule(
                actions=[
                    {"target": "fan", "command": "set_duty"},
                    {"target": "log", "command": "info", "params": {"message": "hot!"}},
                ],
            ),
        ])
        engine = RulesEngine(config, registry)
        fired = await engine.process_event("metrics_updated", {})
        assert fired == ["test"]
        assert engine.stats.actions_executed == 2

    async def test_multiple_events(self, registry: ActionRegistry) -> None:
        config = _make_config([
            _simple_rule(name="rule-a", event="event_a"),
            _simple_rule(name="rule-b", event="event_b"),
        ])
        engine = RulesEngine(config, registry)

        fired_a = await engine.process_event("event_a", {})
        fired_b = await engine.process_event("event_b", {})
        assert fired_a == ["rule-a"]
        assert fired_b == ["rule-b"]

    async def test_reload_preserves_cooldowns(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule(cooldown=300.0)])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})

        # Reload with same rule
        new_config = _make_config([_simple_rule(cooldown=300.0)])
        engine.reload(new_config)

        # Cooldown should still be active
        fired = await engine.process_event("metrics_updated", {})
        assert fired == []

    async def test_reload_removes_stale_cooldowns(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule(name="old-rule", cooldown=300.0)])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})

        # Reload without the old rule
        new_config = _make_config([_simple_rule(name="new-rule")])
        engine.reload(new_config)
        assert "old-rule" not in engine._cooldowns

    async def test_get_rules_for_event(self, registry: ActionRegistry) -> None:
        config = _make_config([
            _simple_rule(name="a", event="temp"),
            _simple_rule(name="b", event="temp"),
            _simple_rule(name="c", event="disk"),
        ])
        engine = RulesEngine(config, registry)
        rules = engine.get_rules_for_event("temp")
        assert [r.name for r in rules] == ["a", "b"]

    async def test_stats_tracking(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule()])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})
        await engine.process_event("metrics_updated", {})

        assert engine.stats.events_processed == 2
        assert engine.stats.actions_executed == 2
        assert engine.stats.last_latency_ms >= 0

    async def test_latency_tracking(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule()])
        engine = RulesEngine(config, registry)
        await engine.process_event("metrics_updated", {})
        # Latency should be a positive number (in ms)
        assert engine.stats.last_latency_ms >= 0

    def test_rules_property(self, registry: ActionRegistry) -> None:
        config = _make_config([_simple_rule(name="a"), _simple_rule(name="b")])
        engine = RulesEngine(config, registry)
        assert len(engine.rules) == 2

    def test_config_property(self, registry: ActionRegistry) -> None:
        config = _make_config([])
        engine = RulesEngine(config, registry)
        assert engine.config is config
