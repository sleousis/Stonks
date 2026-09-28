"""The factor expression language (roadmap 22.2): parsing, analysis and
point-in-time checks. SQL results are checked in test_factor_sql.py."""

from __future__ import annotations

import pytest

from stonks.factors.expression import (
    DIMLESS,
    Call,
    Const,
    Dim,
    ExpressionError,
    Field,
    dims,
    lead,
    lookback,
    next_open_label,
    operators,
    parse,
    parse_factor,
)


def test_parse_builds_a_canonical_tree():
    node = parse("Mean($close, 5) / $close")
    assert node == Call("Div", (Call("Mean", (Field("close"), Const(5.0))), Field("close")))
    assert str(node) == "Div(Mean($close,5),$close)"


def test_canonical_form_ignores_spacing_and_operator_syntax():
    a = parse("($close-$open)/($high-$low+1e-12)")
    b = parse("Div( Sub($close,$open) , Add(Sub($high,$low), 1e-12) )")
    assert str(a) == str(b)


def test_comparisons_logic_and_if():
    node = parse("If(($close > $open) & ($volume >= 1), 1, -1)")
    assert str(node) == "If(And(Gt($close,$open),Ge($volume,1)),1,-1)"
    assert str(parse("-$close")) == "Neg($close)"
    assert str(parse("-2")) == "-2"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("$price", "unknown field"),
        ("Foo($close, 3)", "unknown operator"),
        ("Mean($close)", "takes"),
        ("Mean($close, 2.5)", "whole number"),
        ("Mean($close, 0)", "at least 1"),
        ("Std($close, 1)", "at least 2"),
        ("Quantile($close, 5, 1.5)", "between 0 and 1"),
        ("Mean($close, window=5)", "keyword"),
        ("$close ** 2", "not supported"),
        ("1 < $close < 2", "chained"),
        ("__import__('os')", "unknown operator"),
        ("x", "unknown name"),
        ("Mean($close, 5001)", "at most"),
        ("", "empty"),
        ("Mean($close,", "syntax"),
        ("'text'", "number"),
        ("True", "number"),
    ],
)
def test_bad_expressions_raise_a_clear_error(text, message):
    with pytest.raises(ExpressionError, match=message):
        parse(text)


def test_size_limits_guard_against_huge_input():
    with pytest.raises(ExpressionError, match="too long"):
        parse("$close+" * 1000 + "$close")
    deep = "$close"
    for _ in range(300):
        deep = f"Abs({deep})"
    with pytest.raises(ExpressionError, match="too many"):
        parse(deep)


def test_lookback_counts_bars_before_the_date():
    assert lookback(parse("$close")) == 0
    assert lookback(parse("Ref($close, 5)")) == 5
    assert lookback(parse("Mean($close, 5)")) == 4
    assert lookback(parse("Mean(Ref($close, 1), 5)")) == 5
    assert lookback(parse("Delta($close, 3)")) == 3
    assert lookback(parse("Corr($close, Ref($volume, 2), 10)")) == 11
    assert lookback(parse("CSRank(Mean($close, 3))")) == 2


def test_lead_counts_future_bars_and_factors_refuse_them():
    assert lead(parse("Ref($close, -2)")) == 2
    assert lead(parse("Mean(Ref($open, -1), 3)")) == 1
    with pytest.raises(ExpressionError, match="future"):
        parse_factor("Ref($close, -1) / $close")
    assert str(parse_factor("Ref($close, 1) / $close")) == "Div(Ref($close,1),$close)"


def test_next_open_label_matches_the_signal_convention():
    label = next_open_label(5)
    assert str(label) == "Sub(Div(Ref($open,-6),Ref($open,-1)),1)"
    assert lead(label) == 6
    with pytest.raises(ValueError):
        next_open_label(0)


def test_dimensions_of_common_factors():
    assert dims(parse("$close")) == Dim("pow", 1, 0)
    assert dims(parse("$close * $volume")) == Dim("pow", 1, 1)
    assert dims(parse("Mean($close, 5) / $close")) == DIMLESS
    assert dims(parse("($close-$open)/($high-$low+1e-12)")) == DIMLESS
    assert dims(parse("Log($volume)")) == Dim("log", 0, 1)
    assert dims(parse("Delta(Log($close), 5)")) == DIMLESS
    assert dims(parse("Log($close) - Log(Ref($close, 1))")) == DIMLESS
    assert dims(parse("Corr($close, Log($volume), 10)")) == DIMLESS
    assert dims(parse("Greater($close - Ref($close, 1), 0)")) == Dim("pow", 1, 0)
    assert dims(parse("Sum(Log($close), 3)")) == Dim("log", 3, 0)
    assert dims(parse("CSRank($close)")) == DIMLESS
    assert dims(parse("Rank($close, 5)")) == DIMLESS


def test_expressions_that_depend_on_later_splits_are_refused():
    for text in (
        "$close > 10",
        "$close + $volume",
        "Sign(Log($close))",
        "Log($close) * $close",
        "Log($volume + 1)",
    ):
        with pytest.raises(ExpressionError, match="price or volume level"):
            parse_factor(text)


def test_operator_catalog_describes_every_operator():
    ops = operators()
    names = {o["name"] for o in ops}
    for name in (
        "Ref",
        "Mean",
        "Std",
        "Rank",
        "CSRank",
        "Corr",
        "Delta",
        "Log",
        "Abs",
        "Sign",
        "If",
        "Min",
        "Max",
        "Greater",
        "Less",
        "Sum",
        "Quantile",
        "Slope",
        "Rsquare",
        "Resi",
        "IdxMax",
        "IdxMin",
    ):
        assert name in names
    assert all(o["summary"] for o in ops)
