"""Pydantic v2 models for automation rules.

Rules are declared in config.yaml under ``automation.rules`` and follow
a condition → action pattern triggered by EventBus events.

Priority classes enforce conflict resolution: safety > scheduled > user.
Maximum 100 rules allowed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class RulePriority(StrEnum):
    """Priority class for conflict resolution.

    Higher priority rules take precedence when multiple rules target the
    same resource.  Order: safety > scheduled > user.
    """

    SAFETY = "safety"
    SCHEDULED = "scheduled"
    USER = "user"


PRIORITY_ORDER: dict[RulePriority, int] = {
    RulePriority.SAFETY: 100,
    RulePriority.SCHEDULED: 50,
    RulePriority.USER: 10,
}


class ConditionOperator(StrEnum):
    """Supported comparison operators for conditions."""

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"


class RuleCondition(BaseModel):
    """A single condition that must be satisfied for a rule to fire.

    Evaluates ``event_data[field] <operator> value``.
    """

    field: str = Field(description="Dot-path into event data (e.g. 'cpu_temp')")
    operator: ConditionOperator = Field(description="Comparison operator")
    value: Any = Field(description="Reference value for comparison")

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: Any) -> Any:
        # Allow numbers, strings, lists, bools — reject complex objects
        if isinstance(v, (int, float, str, bool, list)):
            return v
        msg = f"Condition value must be a number, string, bool, or list — got {type(v).__name__}"
        raise ValueError(msg)


class RuleAction(BaseModel):
    """An action to execute when a rule fires.

    The ``target`` identifies the subsystem (e.g. ``"fan"``, ``"led"``,
    ``"emit"``), and ``command`` + ``params`` specify what to do.
    """

    target: str = Field(description="Subsystem target (fan, led, oled, emit, log)")
    command: str = Field(description="Action command (e.g. 'set_duty', 'set_mode')")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the command",
    )


class AutomationRule(BaseModel):
    """A single automation rule: event trigger + conditions + actions.

    Rules listen for a specific event, evaluate all conditions (AND logic),
    and execute actions when all conditions are met.
    """

    name: str = Field(description="Unique human-readable rule name")
    description: str = Field(default="", description="Optional description")
    enabled: bool = Field(default=True, description="Whether this rule is active")
    priority: RulePriority = Field(
        default=RulePriority.USER,
        description="Priority class for conflict resolution",
    )
    event: str = Field(description="EventBus event to listen for")
    conditions: list[RuleCondition] = Field(
        default_factory=list,
        description="All conditions must be true (AND logic)",
    )
    actions: list[RuleAction] = Field(
        description="Actions to execute when conditions are met",
    )
    cooldown: float = Field(
        default=0.0,
        ge=0.0,
        le=3600.0,
        description="Minimum seconds between consecutive firings",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            msg = "Rule name must not be empty"
            raise ValueError(msg)
        if len(v) > 64:
            msg = "Rule name must be 64 characters or fewer"
            raise ValueError(msg)
        return v


class AutomationConfig(BaseModel):
    """Top-level automation configuration.

    Placed under ``automation`` in config.yaml.
    """

    enabled: bool = Field(default=False, description="Master enable for automation engine")
    rules: list[AutomationRule] = Field(
        default_factory=list,
        description="List of automation rules (max 100)",
    )

    @field_validator("rules")
    @classmethod
    def _validate_rules_limit(cls, v: list[AutomationRule]) -> list[AutomationRule]:
        if len(v) > 100:
            msg = f"Maximum 100 automation rules allowed, got {len(v)}"
            raise ValueError(msg)
        # Validate unique names
        names = [r.name for r in v]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                msg = f"Duplicate rule name: '{name}'"
                raise ValueError(msg)
            seen.add(name)
        return v

    @model_validator(mode="after")
    def _validate_config(self) -> AutomationConfig:
        """Ensure rules are sorted by priority (highest first) for deterministic evaluation.

        Secondary sort is alphabetical by name (ascending) so that rules at the
        same priority level have a stable, predictable evaluation order.
        """
        self.rules = sorted(
            self.rules,
            key=lambda r: (-PRIORITY_ORDER.get(r.priority, 0), r.name),
        )
        return self
