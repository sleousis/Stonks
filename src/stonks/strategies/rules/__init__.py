"""Declarative rule specs for :class:`stonks.strategies.rule_based.RuleStrategy`:
the spec model and its validation, the indicator registry, the condition
interpreter and starter templates."""

from stonks.strategies.rules.spec import (
    INDICATOR_KINDS,
    RuleSpec,
    RuleSpecError,
    SpecIssue,
    rule_spec_json_schema,
    validate_spec,
)
from stonks.strategies.rules.templates import TEMPLATES, RuleTemplate

__all__ = [
    "INDICATOR_KINDS",
    "TEMPLATES",
    "RuleSpec",
    "RuleSpecError",
    "RuleTemplate",
    "SpecIssue",
    "rule_spec_json_schema",
    "validate_spec",
]
