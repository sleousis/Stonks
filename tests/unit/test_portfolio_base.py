"""PortfolioConstructor seam: input/book types and the auto-discovered registry."""

from __future__ import annotations

import sys
import textwrap
from datetime import date

import pytest

from stonks.core.types import Portfolio
from stonks.portfolio import base
from stonks.portfolio.base import (
    ConstructionInput,
    PortfolioConstructor,
    TargetBook,
    constructor_names,
    get_constructor,
    register_constructor,
)


def _inp(**kw) -> ConstructionInput:
    defaults = {
        "signals": {"a": {"X": 1.0}},
        "portfolio": Portfolio(cash=100.0),
        "prices": {"X": 10.0},
        "as_of": date(2026, 1, 2),
    }
    defaults.update(kw)
    return ConstructionInput(**defaults)


def test_registry_lists_every_builtin_constructor() -> None:
    assert {"single_winner", "equal_weight_top_n", "inverse_vol", "vol_target"} <= set(
        constructor_names()
    )


def test_unknown_name_raises_and_lists_valid_ones() -> None:
    with pytest.raises(ValueError, match="inverse_vol"):
        get_constructor("nope")


def test_get_constructor_validates_settings() -> None:
    c = get_constructor("equal_weight_top_n", n=3)
    assert c.settings.n == 3
    assert c.name == "equal_weight_top_n"
    with pytest.raises(ValueError):
        get_constructor("equal_weight_top_n", n=0)
    with pytest.raises(ValueError):
        get_constructor("equal_weight_top_n", bogus=1)


def test_every_constructor_declares_its_signal_method() -> None:
    from stonks.portfolio.signals import normalizer_names

    for name in constructor_names():
        method = get_constructor(name).signal_method
        assert method == "raw" or method in normalizer_names()


def test_duplicate_registration_raises() -> None:
    constructor_names()  # make sure the built-ins are registered
    with pytest.raises(ValueError, match="already registered"):

        @register_constructor("equal_weight_top_n")
        class _Dup(PortfolioConstructor):
            def target_weights(self, inp):  # pragma: no cover
                return TargetBook()


def test_a_new_module_is_discovered_without_editing_any_list(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "extra_constructors"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "all_cash.py").write_text(
        textwrap.dedent(
            """
            from stonks.portfolio.base import PortfolioConstructor, TargetBook, register_constructor

            @register_constructor("all_cash_test")
            class AllCash(PortfolioConstructor):
                def target_weights(self, inp):
                    return TargetBook()
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        base.discover_constructors("extra_constructors")
        assert "all_cash_test" in constructor_names()
        assert get_constructor("all_cash_test").target_weights(_inp()).weights == {}
    finally:
        base._REGISTRY.pop("all_cash_test", None)
        for mod in [m for m in sys.modules if m.startswith("extra_constructors")]:
            del sys.modules[mod]


def test_strategy_weights_default_to_equal_and_normalise() -> None:
    inp = _inp(signals={"a": {}, "b": {}})
    assert inp.normalized_strategy_weights() == {"a": 0.5, "b": 0.5}
    inp = _inp(signals={"a": {}, "b": {}}, strategy_weights={"a": 3.0, "b": 1.0})
    assert inp.normalized_strategy_weights() == {"a": 0.75, "b": 0.25}


def test_strategy_weights_reject_negatives() -> None:
    with pytest.raises(ValueError):
        _inp(signals={"a": {}}, strategy_weights={"a": -1.0}).normalized_strategy_weights()


def test_target_book_reports_gross_and_net() -> None:
    book = TargetBook(weights={"X": 0.25, "Y": 0.5})
    assert book.gross == pytest.approx(0.75)
    assert book.net == pytest.approx(0.75)
    assert book.cash_weight == pytest.approx(0.25)


def test_target_book_rejects_non_finite_weights() -> None:
    with pytest.raises(ValueError):
        TargetBook(weights={"X": float("nan")})
