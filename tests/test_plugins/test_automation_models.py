"""Tests for automation rules engine Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from casectl.plugins.automation.models import (
    AutomationConfig,
    AutomationRule,
    ConditionOperator,
    PRIORITY_ORDER,
    RuleAction,
    RuleCondition,
    RulePriority,
)


class TestRulePriority:
    """Tests for priority ordering."""

    def test_priority_order_values(self) -> None:
        assert PRIORITY_ORDER[RulePriority.SAFETY] > PRIORITY_ORDER[RulePriority.SCHEDULED]
        assert PRIORITY_ORDER[RulePriority.SCHEDULED] > PRIORITY_ORDER[RulePriority.USER]

    def test_priority_enum_values(self) -> None:
        assert RulePriority.SAFETY == "safety"
        assert RulePriority.SCHEDULED == "scheduled"
        assert RulePriority.USER == "user"


class TestConditionOperator:
    """Tests for condition operator enum."""

    def test_all_operators_defined(self) -> None:
        ops = {op.value for op in ConditionOperator}
        assert ops == {"gt", "gte", "lt", "lte", "eq", "neq", "in", "not_in", "between"}


class TestRuleCondition:
    """Tests for RuleCondition model validation."""

    def test_valid_numeric_condition(self) -> None:
        c = RuleCondition(field="cpu_temp", operator=ConditionOperator.GT, value=80.0)
        assert c.field == "cpu_temp"
        assert c.operator == ConditionOperator.GT
        assert c.value == 80.0

    def test_valid_string_condition(self) -> None:
        c = RuleCondition(field="fan.mode", operator=ConditionOperator.EQ, value="manual")
        assert c.value == "manual"

    def test_valid_list_condition(self) -> None:
        c = RuleCondition(field="status", operator=ConditionOperator.IN, value=["ok", "warning"])
        assert c.value == ["ok", "warning"]

    def test_valid_between_condition(self) -> None:
        c = RuleCondition(field="temp", operator=ConditionOperator.BETWEEN, value=[30, 60])
        assert c.value == [30, 60]

    def test_valid_bool_condition(self) -> None:
        c = RuleCondition(field="enabled", operator=ConditionOperator.EQ, value=True)
        assert c.value is True

    def test_rejects_dict_value(self) -> None:
        with pytest.raises(ValidationError, match="number, string, bool, or list"):
            RuleCondition(field="x", operator=ConditionOperator.EQ, value={"a": 1})


class TestRuleAction:
    """Tests for RuleAction model."""

    def test_valid_action(self) -> None:
        a = RuleAction(target="fan", command="set_duty", params={"duty": [200, 200, 200]})
        assert a.target == "fan"
        assert a.command == "set_duty"
        assert a.params["duty"] == [200, 200, 200]

    def test_action_default_params(self) -> None:
        a = RuleAction(target="log", command="info")
        assert a.params == {}


class TestAutomationRule:
    """Tests for AutomationRule model validation."""

    def _make_rule(self, **overrides) -> AutomationRule:
        defaults = {
            "name": "test-rule",
            "event": "metrics_updated",
            "actions": [{"target": "log", "command": "info"}],
        }
        defaults.update(overrides)
        return AutomationRule.model_validate(defaults)

    def test_minimal_rule(self) -> None:
        rule = self._make_rule()
        assert rule.name == "test-rule"
        assert rule.enabled is True
        assert rule.priority == RulePriority.USER
        assert rule.cooldown == 0.0
        assert rule.conditions == []

    def test_full_rule(self) -> None:
        rule = self._make_rule(
            description="Overheat protection",
            priority="safety",
            cooldown=30.0,
            conditions=[{"field": "cpu_temp", "operator": "gt", "value": 85}],
        )
        assert rule.priority == RulePriority.SAFETY
        assert rule.cooldown == 30.0
        assert len(rule.conditions) == 1

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            self._make_rule(name="")

    def test_whitespace_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            self._make_rule(name="   ")

    def test_long_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="64 characters"):
            self._make_rule(name="x" * 65)

    def test_cooldown_max(self) -> None:
        with pytest.raises(ValidationError):
            self._make_rule(cooldown=3601)

    def test_cooldown_negative(self) -> None:
        with pytest.raises(ValidationError):
            self._make_rule(cooldown=-1)


class TestAutomationConfig:
    """Tests for AutomationConfig model validation."""

    def test_default_config(self) -> None:
        cfg = AutomationConfig()
        assert cfg.enabled is False
        assert cfg.rules == []

    def test_config_with_rules(self) -> None:
        cfg = AutomationConfig(
            enabled=True,
            rules=[
                {
                    "name": "rule1",
                    "event": "temp",
                    "priority": "user",
                    "actions": [{"target": "log", "command": "info"}],
                },
                {
                    "name": "rule2",
                    "event": "temp",
                    "priority": "safety",
                    "actions": [{"target": "fan", "command": "set_duty"}],
                },
            ],
        )
        assert len(cfg.rules) == 2
        # Rules sorted by priority: safety first
        assert cfg.rules[0].name == "rule2"
        assert cfg.rules[1].name == "rule1"

    def test_max_100_rules(self) -> None:
        rules = [
            {"name": f"rule-{i}", "event": "e", "actions": [{"target": "log", "command": "x"}]}
            for i in range(100)
        ]
        cfg = AutomationConfig(rules=rules)
        assert len(cfg.rules) == 100

    def test_over_100_rules_rejected(self) -> None:
        rules = [
            {"name": f"rule-{i}", "event": "e", "actions": [{"target": "log", "command": "x"}]}
            for i in range(101)
        ]
        with pytest.raises(ValidationError, match="Maximum 100"):
            AutomationConfig(rules=rules)

    def test_duplicate_rule_names_rejected(self) -> None:
        rules = [
            {"name": "dup", "event": "e1", "actions": [{"target": "log", "command": "x"}]},
            {"name": "dup", "event": "e2", "actions": [{"target": "log", "command": "y"}]},
        ]
        with pytest.raises(ValidationError, match="Duplicate rule name"):
            AutomationConfig(rules=rules)

    def test_priority_sorting(self) -> None:
        """Rules with mixed priorities should be sorted: safety > scheduled > user."""
        rules = [
            {
                "name": "low",
                "event": "e",
                "priority": "user",
                "actions": [{"target": "log", "command": "x"}],
            },
            {
                "name": "high",
                "event": "e",
                "priority": "safety",
                "actions": [{"target": "log", "command": "x"}],
            },
            {
                "name": "mid",
                "event": "e",
                "priority": "scheduled",
                "actions": [{"target": "log", "command": "x"}],
            },
        ]
        cfg = AutomationConfig(rules=rules)
        priorities = [r.priority for r in cfg.rules]
        assert priorities == [RulePriority.SAFETY, RulePriority.SCHEDULED, RulePriority.USER]

    def test_same_priority_sorted_by_name(self) -> None:
        """Rules at the same priority level are sorted alphabetically by name."""
        rules = [
            {
                "name": "charlie",
                "event": "e",
                "priority": "user",
                "actions": [{"target": "log", "command": "x"}],
            },
            {
                "name": "alpha",
                "event": "e",
                "priority": "user",
                "actions": [{"target": "log", "command": "x"}],
            },
            {
                "name": "bravo",
                "event": "e",
                "priority": "user",
                "actions": [{"target": "log", "command": "x"}],
            },
        ]
        cfg = AutomationConfig(rules=rules)
        names = [r.name for r in cfg.rules]
        assert names == ["alpha", "bravo", "charlie"]

    def test_mixed_priority_and_name_sorting(self) -> None:
        """Priority is primary sort key, name is secondary."""
        rules = [
            {
                "name": "z-user",
                "event": "e",
                "priority": "user",
                "actions": [{"target": "log", "command": "x"}],
            },
            {
                "name": "b-safety",
                "event": "e",
                "priority": "safety",
                "actions": [{"target": "log", "command": "x"}],
            },
            {
                "name": "a-safety",
                "event": "e",
                "priority": "safety",
                "actions": [{"target": "log", "command": "x"}],
            },
            {
                "name": "a-user",
                "event": "e",
                "priority": "user",
                "actions": [{"target": "log", "command": "x"}],
            },
        ]
        cfg = AutomationConfig(rules=rules)
        names = [r.name for r in cfg.rules]
        assert names == ["a-safety", "b-safety", "a-user", "z-user"]
