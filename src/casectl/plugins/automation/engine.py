"""Core automation rules engine.

Evaluates conditions against EventBus event data and executes actions
with priority-based conflict resolution and cooldown tracking.

Latency target: < 500ms from event emission to action execution.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from casectl.plugins.automation.models import (
    PRIORITY_ORDER,
    AutomationConfig,
    AutomationRule,
    ConditionOperator,
    RuleAction,
    RuleCondition,
)

logger = logging.getLogger(__name__)


def _resolve_field(data: Any, field_path: str) -> Any:
    """Resolve a dot-separated field path against nested data.

    Supports both dict-like access and attribute access.

    Examples::

        _resolve_field({"cpu_temp": 75.0}, "cpu_temp") → 75.0
        _resolve_field({"fan": {"duty": [100]}}, "fan.duty") → [100]

    Raises:
        KeyError: If the field path cannot be resolved.
    """
    parts = field_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"Field '{part}' not found in data at path '{field_path}'")
            current = current[part]
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            raise KeyError(f"Field '{part}' not found in data at path '{field_path}'")
    return current


def evaluate_condition(condition: RuleCondition, data: Any) -> bool:
    """Evaluate a single condition against event data.

    Parameters
    ----------
    condition:
        The condition to evaluate.
    data:
        Event payload (typically a dict).

    Returns
    -------
    bool
        ``True`` if the condition is satisfied.
    """
    try:
        actual = _resolve_field(data, condition.field)
    except (KeyError, TypeError, AttributeError):
        logger.debug(
            "Condition field '%s' not found in event data — condition fails",
            condition.field,
        )
        return False

    op = condition.operator
    expected = condition.value

    try:
        if op == ConditionOperator.GT:
            return float(actual) > float(expected)
        if op == ConditionOperator.GTE:
            return float(actual) >= float(expected)
        if op == ConditionOperator.LT:
            return float(actual) < float(expected)
        if op == ConditionOperator.LTE:
            return float(actual) <= float(expected)
        if op == ConditionOperator.EQ:
            return actual == expected
        if op == ConditionOperator.NEQ:
            return actual != expected
        if op == ConditionOperator.IN:
            if not isinstance(expected, list):
                logger.warning("'in' operator requires a list value, got %s", type(expected))
                return False
            return actual in expected
        if op == ConditionOperator.NOT_IN:
            if not isinstance(expected, list):
                logger.warning("'not_in' operator requires a list value, got %s", type(expected))
                return False
            return actual not in expected
        if op == ConditionOperator.BETWEEN:
            if not isinstance(expected, list) or len(expected) != 2:
                logger.warning("'between' operator requires a [min, max] list")
                return False
            return float(expected[0]) <= float(actual) <= float(expected[1])
    except (TypeError, ValueError) as exc:
        logger.debug("Condition evaluation error for '%s': %s", condition.field, exc)
        return False

    return False


def evaluate_conditions(conditions: list[RuleCondition], data: Any) -> bool:
    """Evaluate all conditions (AND logic).

    Returns ``True`` only if *all* conditions pass.  An empty condition
    list is always ``True`` (unconditional rule).
    """
    return all(evaluate_condition(c, data) for c in conditions)


# ---------------------------------------------------------------------------
# Action handler registry
# ---------------------------------------------------------------------------

ActionHandler = Callable[[RuleAction], Coroutine[Any, Any, None]]


class ActionRegistry:
    """Registry of action handlers keyed by target name.

    Plugins register handlers for their target (e.g. ``"fan"``, ``"led"``),
    and the engine dispatches actions to the appropriate handler.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ActionHandler] = {}

    def register(self, target: str, handler: ActionHandler) -> None:
        """Register *handler* for actions targeting *target*."""
        self._handlers[target] = handler
        logger.debug("Registered action handler for target '%s'", target)

    def unregister(self, target: str) -> None:
        """Remove the handler for *target*."""
        self._handlers.pop(target, None)

    def get(self, target: str) -> ActionHandler | None:
        """Return the handler for *target*, or ``None``."""
        return self._handlers.get(target)

    @property
    def targets(self) -> list[str]:
        """List of registered target names."""
        return list(self._handlers.keys())


# ---------------------------------------------------------------------------
# Conflict tracking
# ---------------------------------------------------------------------------


class ConflictRecord:
    """Record of a conflict resolution decision.

    Captures which rule won and which rules were suppressed for a
    given target+command key during a single event processing cycle.
    """

    __slots__ = ("conflict_key", "winner_rule", "winner_priority", "suppressed")

    def __init__(
        self,
        conflict_key: str,
        winner_rule: str,
        winner_priority: int,
    ) -> None:
        self.conflict_key = conflict_key
        self.winner_rule = winner_rule
        self.winner_priority = winner_priority
        self.suppressed: list[tuple[str, int]] = []  # (rule_name, priority_value)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for API / logging."""
        return {
            "conflict_key": self.conflict_key,
            "winner_rule": self.winner_rule,
            "winner_priority": self.winner_priority,
            "suppressed": [
                {"rule": name, "priority": prio} for name, prio in self.suppressed
            ],
        }


# ---------------------------------------------------------------------------
# Engine statistics
# ---------------------------------------------------------------------------


class EngineStats:
    """Lightweight counters for engine diagnostics."""

    def __init__(self) -> None:
        self.events_processed: int = 0
        self.actions_executed: int = 0
        self.actions_failed: int = 0
        self.conditions_failed: int = 0
        self.skipped_cooldown: int = 0
        self.handler_missing: int = 0
        self.conflicts_resolved: int = 0
        self.last_latency_ms: float = 0.0
        self.last_conflicts: list[ConflictRecord] = []

    def to_dict(self) -> dict[str, Any]:
        """Return stats as a plain dict."""
        return {
            "events_processed": self.events_processed,
            "actions_executed": self.actions_executed,
            "actions_failed": self.actions_failed,
            "conditions_failed": self.conditions_failed,
            "skipped_cooldown": self.skipped_cooldown,
            "handler_missing": self.handler_missing,
            "conflicts_resolved": self.conflicts_resolved,
            "last_latency_ms": round(self.last_latency_ms, 2),
            "last_conflicts": [c.to_dict() for c in self.last_conflicts],
        }


# ---------------------------------------------------------------------------
# Rules engine
# ---------------------------------------------------------------------------


class RulesEngine:
    """Event-driven automation rules engine.

    Manages a set of :class:`AutomationRule` instances, subscribes to
    EventBus events, evaluates conditions, resolves conflicts by priority,
    and dispatches actions.

    Parameters
    ----------
    config:
        The automation configuration containing rules.
    action_registry:
        Registry of action handlers for dispatching.
    """

    def __init__(
        self,
        config: AutomationConfig,
        action_registry: ActionRegistry,
    ) -> None:
        self._config = config
        self._action_registry = action_registry
        # Cooldown tracking: rule_name → last_fired_timestamp
        self._cooldowns: dict[str, float] = {}
        # Conflict tracking: target+command → (priority, rule_name)
        # Reset each evaluation cycle
        self._active_rules: dict[str, AutomationRule] = {r.name: r for r in config.rules}
        self._stats = EngineStats()

    @property
    def config(self) -> AutomationConfig:
        """Current automation configuration."""
        return self._config

    @property
    def action_registry(self) -> ActionRegistry:
        """The action handler registry."""
        return self._action_registry

    @property
    def stats(self) -> EngineStats:
        """Engine statistics."""
        return self._stats

    @property
    def rules(self) -> list[AutomationRule]:
        """Current active rules (sorted by priority)."""
        return self._config.rules

    def reload(self, config: AutomationConfig) -> None:
        """Replace the current configuration with a new one.

        Preserves cooldown state for rules that still exist by name.
        """
        old_cooldowns = dict(self._cooldowns)
        self._config = config
        self._active_rules = {r.name: r for r in config.rules}
        # Preserve cooldowns for rules that still exist
        self._cooldowns = {
            name: ts for name, ts in old_cooldowns.items() if name in self._active_rules
        }
        logger.info("Reloaded automation config: %d rules", len(config.rules))

    def get_rules_for_event(self, event: str) -> list[AutomationRule]:
        """Return all enabled rules that listen for *event*, sorted by priority."""
        return [
            r
            for r in self._config.rules
            if r.enabled and r.event == event
        ]

    def _check_cooldown(self, rule: AutomationRule) -> bool:
        """Return ``True`` if the rule is NOT in cooldown (i.e. can fire)."""
        if rule.cooldown <= 0:
            return True
        last_fired = self._cooldowns.get(rule.name, 0.0)
        return (time.monotonic() - last_fired) >= rule.cooldown

    def _record_cooldown(self, rule: AutomationRule) -> None:
        """Record the current time as the last firing time for *rule*."""
        if rule.cooldown > 0:
            self._cooldowns[rule.name] = time.monotonic()

    async def process_event(self, event: str, data: Any) -> list[str]:
        """Process an event through the rules engine.

        Evaluates all matching rules, resolves priority conflicts for
        overlapping targets, and executes winning actions.

        Parameters
        ----------
        event:
            The event name.
        data:
            The event payload.

        Returns
        -------
        list[str]
            Names of rules that fired.
        """
        if not self._config.enabled:
            return []

        start_time = time.monotonic()
        matching_rules = self.get_rules_for_event(event)
        if not matching_rules:
            return []

        fired: list[str] = []

        # Conflict resolution: collect actions per target+command,
        # only highest-priority rule wins for each target+command.
        # Rules are already sorted by priority desc, then name asc,
        # so the first match for a given conflict key always wins
        # deterministically.
        winning_actions: dict[str, tuple[int, AutomationRule, RuleAction]] = {}
        # Track conflict records for diagnostics
        conflict_records: dict[str, ConflictRecord] = {}

        for rule in matching_rules:
            # Check cooldown
            if not self._check_cooldown(rule):
                logger.debug("Rule '%s' skipped — in cooldown", rule.name)
                self._stats.skipped_cooldown += 1
                continue

            # Evaluate conditions
            if not evaluate_conditions(rule.conditions, data):
                logger.debug("Rule '%s' conditions not met", rule.name)
                self._stats.conditions_failed += 1
                continue

            # Collect actions with priority
            priority_val = PRIORITY_ORDER.get(rule.priority, 0)
            for action in rule.actions:
                conflict_key = f"{action.target}:{action.command}"
                existing = winning_actions.get(conflict_key)
                if existing is None:
                    winning_actions[conflict_key] = (priority_val, rule, action)
                    conflict_records[conflict_key] = ConflictRecord(
                        conflict_key=conflict_key,
                        winner_rule=rule.name,
                        winner_priority=priority_val,
                    )
                elif priority_val > existing[0]:
                    # Higher priority takes over — record the demotion
                    old_record = conflict_records[conflict_key]
                    old_record.suppressed.append(
                        (old_record.winner_rule, old_record.winner_priority)
                    )
                    winning_actions[conflict_key] = (priority_val, rule, action)
                    conflict_records[conflict_key] = ConflictRecord(
                        conflict_key=conflict_key,
                        winner_rule=rule.name,
                        winner_priority=priority_val,
                    )
                    # Carry over any previously suppressed rules
                    conflict_records[conflict_key].suppressed = old_record.suppressed
                    self._stats.conflicts_resolved += 1
                    logger.info(
                        "Conflict on '%s': rule '%s' (priority %d) overrides '%s' (priority %d)",
                        conflict_key,
                        rule.name,
                        priority_val,
                        existing[1].name,
                        existing[0],
                    )
                else:
                    # Same or lower priority — current winner stands.
                    # Record suppression for diagnostics.
                    record = conflict_records[conflict_key]
                    record.suppressed.append((rule.name, priority_val))
                    if priority_val == existing[0]:
                        logger.debug(
                            "Conflict on '%s': rule '%s' suppressed (same priority as '%s', "
                            "deterministic tie-break by name)",
                            conflict_key,
                            rule.name,
                            existing[1].name,
                        )
                    else:
                        logger.debug(
                            "Conflict on '%s': rule '%s' (priority %d) suppressed by '%s' (priority %d)",
                            conflict_key,
                            rule.name,
                            priority_val,
                            existing[1].name,
                            existing[0],
                        )
                    self._stats.conflicts_resolved += 1

        # Store conflict records that actually had suppressions
        self._stats.last_conflicts = [
            r for r in conflict_records.values() if r.suppressed
        ]

        # Execute winning actions
        executed_rules: set[str] = set()
        for _priority_val, rule, action in winning_actions.values():
            handler = self._action_registry.get(action.target)
            if handler is None:
                logger.warning(
                    "No action handler for target '%s' (rule '%s')",
                    action.target,
                    rule.name,
                )
                self._stats.handler_missing += 1
                continue

            try:
                await handler(action)
                executed_rules.add(rule.name)
                logger.debug(
                    "Executed action %s:%s from rule '%s'",
                    action.target,
                    action.command,
                    rule.name,
                )
                self._stats.actions_executed += 1
            except Exception:
                logger.error(
                    "Action %s:%s from rule '%s' failed",
                    action.target,
                    action.command,
                    rule.name,
                    exc_info=True,
                )
                self._stats.actions_failed += 1

        # Record cooldowns and fired rules
        for rule_name in executed_rules:
            rule = self._active_rules[rule_name]
            self._record_cooldown(rule)
            fired.append(rule_name)

        elapsed_ms = (time.monotonic() - start_time) * 1000
        self._stats.events_processed += 1
        self._stats.last_latency_ms = elapsed_ms

        if elapsed_ms > 500:
            logger.warning(
                "Automation latency %.1fms exceeds 500ms target for event '%s'",
                elapsed_ms,
                event,
            )

        logger.debug(
            "Processed event '%s': %d rules matched, %d fired in %.1fms",
            event,
            len(matching_rules),
            len(fired),
            elapsed_ms,
        )

        return fired
