"""RuleSpec: the validated JSON spec behind RuleStrategy."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from stonks.strategies.rules import (
    TEMPLATES,
    RuleSpec,
    RuleSpecError,
    rule_spec_json_schema,
    validate_spec,
)


def _base() -> dict:
    return {
        "version": 1,
        "name": "test",
        "interval": "1d",
        "universe": {"asset_classes": ["equity"]},
        "indicators": [
            {"id": "fast", "kind": "sma", "period": 5},
            {"id": "slow", "kind": "sma", "period": 20},
        ],
        "entry": {
            "type": "compare",
            "left": {"type": "indicator", "id": "fast"},
            "op": ">",
            "right": {"type": "indicator", "id": "slow"},
        },
        "rank": {"by": "fast"},
    }


def _paths(exc: RuleSpecError) -> list[str]:
    return [i.path for i in exc.issues]


def test_minimal_spec_validates_with_defaults():
    spec = validate_spec(_base())
    assert isinstance(spec, RuleSpec)
    assert spec.exit is None
    assert spec.exit_when_entry_false is False
    assert spec.sizing.max_positions == 5
    assert spec.sizing.allocation == 1.0
    assert spec.risk.stop_loss_pct is None
    assert spec.rank.order == "desc"


def test_accepts_json_string():
    assert validate_spec(json.dumps(_base())).name == "test"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_templates_validate(name):
    spec = validate_spec(TEMPLATES[name].spec)
    assert spec.version == 1


def test_three_templates():
    assert {"rsi_mean_reversion", "sma_trend_following", "donchian_breakout"} == set(TEMPLATES)


def test_round_trip_dump_is_stable():
    spec = validate_spec(_base())
    dumped = spec.model_dump(mode="json")
    assert validate_spec(dumped).model_dump(mode="json") == dumped


def test_unknown_indicator_reference_has_precise_path():
    data = _base()
    data["entry"] = {
        "type": "all",
        "conditions": [
            {
                "type": "compare",
                "left": {"type": "indicator", "id": "fast"},
                "op": ">",
                "right": {"type": "constant", "value": 1},
            },
            {
                "type": "not",
                "condition": {
                    "type": "compare",
                    "left": {"type": "indicator", "id": "nope"},
                    "op": "<",
                    "right": {"type": "constant", "value": 1},
                },
            },
        ],
    }
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["entry.conditions[1].condition.left.id"]
    assert "nope" in exc.value.issues[0].message


def test_unknown_rank_indicator():
    data = _base()
    data["rank"] = {"by": "zzz"}
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["rank.by"]


def test_duplicate_indicator_ids():
    data = _base()
    data["indicators"].append({"id": "fast", "kind": "ema", "period": 3})
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["indicators[2].id"]


def test_unknown_indicator_kind():
    data = _base()
    data["indicators"][0] = {"id": "x", "kind": "macd", "period": 3}
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value)[0] == "indicators[0].kind"


def test_missing_and_bad_indicator_params():
    data = _base()
    data["indicators"][0] = {"id": "fast", "kind": "sma"}
    data["indicators"][1] = {"id": "slow", "kind": "rsi", "period": 1}
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["indicators[0].period", "indicators[1].period"]


def test_bad_operator_path():
    data = _base()
    data["entry"]["op"] = "=="
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["entry.op"]


def test_wrong_version_rejected():
    data = _base()
    data["version"] = 2
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["version"]


def test_unknown_field_rejected():
    data = _base()
    data["code"] = "__import__('os')"
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["code"]


def test_bad_interval_and_asset_class():
    data = _base()
    data["interval"] = "7x"
    data["universe"] = {"asset_classes": ["forex"]}
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert set(_paths(exc.value)) == {"interval", "universe.asset_classes[0]"}


def test_empty_asset_classes_rejected():
    data = _base()
    data["universe"] = {"asset_classes": []}
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["universe.asset_classes"]


def test_sizing_and_risk_bounds():
    data = _base()
    data["sizing"] = {"max_positions": 0, "allocation": 1.5}
    data["risk"] = {"stop_loss_pct": 1.5, "take_profit_pct": -0.1}
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert set(_paths(exc.value)) == {
        "sizing.max_positions",
        "sizing.allocation",
        "risk.stop_loss_pct",
        "risk.take_profit_pct",
    }


def test_excessive_nesting_rejected():
    cond = copy.deepcopy(_base()["entry"])
    for _ in range(40):
        cond = {"type": "not", "condition": cond}
    data = _base()
    data["entry"] = cond
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert _paths(exc.value) == ["entry"]
    assert "deep" in exc.value.issues[0].message


def test_not_an_object():
    with pytest.raises(RuleSpecError) as exc:
        validate_spec("[1, 2]")
    assert _paths(exc.value) == [""]
    with pytest.raises(RuleSpecError):
        validate_spec("{not json")


def test_error_message_lists_every_issue():
    data = _base()
    data["rank"] = {"by": "zzz"}
    data["version"] = 3
    with pytest.raises(RuleSpecError) as exc:
        validate_spec(data)
    assert "rank.by" in str(exc.value) or "version" in str(exc.value)


def test_json_schema_describes_the_builder_vocabulary():
    schema = rule_spec_json_schema()
    text = json.dumps(schema)
    assert schema["title"] == "RuleSpec"
    for token in ("crosses_above", "crosses_below", "donchian_high", "zscore", "atr"):
        assert token in text


def test_rule_modules_never_eval_or_exec():
    root = Path(__file__).resolve().parents[2] / "src" / "stonks" / "strategies"
    files = [root / "rule_based.py", *(root / "rules").glob("*.py")]
    for path in files:
        source = path.read_text(encoding="utf-8")
        for banned in ("eval(", "exec(", "compile(", "__import__"):
            assert banned not in source, (path.name, banned)
