"""Every strategy people can pick has a plain name and one plain line on what
it does, never a code docstring (release polish, audit finding M6)."""

from __future__ import annotations

import re

import pytest

from stonks.app.catalog import CatalogService, LabCatalogSource, plain_summary, plain_title
from stonks.app.studio import RuleStrategySource
from stonks.strategies.base import BaseStrategy

_CODE = re.compile(r":meth:|:class:|``|\bsubclass|\bdocstring\b|\bBL-\d+|_[a-z]+_|\(\)")


@pytest.fixture(scope="module")
def infos():
    return CatalogService([LabCatalogSource(), RuleStrategySource()]).strategies()


def test_every_catalog_entry_has_a_plain_title_and_summary(infos):
    assert len(infos) >= 39
    for info in infos:
        assert info.title, info.class_path
        assert info.description, info.class_path
        assert not _CODE.search(info.description), (info.class_path, info.description)
        assert len(info.description) <= 160, info.class_path
        assert info.description.endswith("."), info.class_path


def test_titles_are_unique(infos):
    titles = [i.title for i in infos]
    assert len(titles) == len(set(titles))


def test_wrappers_are_flagged(infos):
    by_name = {i.name: i for i in infos}
    assert by_name["trailing_stop"].is_wrapper
    assert by_name["regime_filter"].is_wrapper
    assert not by_name["momentum"].is_wrapper
    assert by_name["momentum"].alpha_family == "trend"


def test_summary_falls_back_to_the_hypothesis_then_the_own_docstring():
    class WithHypothesis(BaseStrategy):
        id = "with_hypothesis"
        hypothesis = "Prices trend. Fails in ranges."

    class Base(BaseStrategy):
        """Base of things. Subclasses implement :meth:`_x`."""

    class NoDoc(Base):
        id = "no_doc"

    class OwnDoc(BaseStrategy):
        """Uses :class:`~pkg.mod.Thing` and ``x``.

        More detail."""

    assert plain_summary(WithHypothesis) == "Prices trend."
    assert plain_summary(NoDoc) == ""  # never the parent's docstring
    assert plain_summary(OwnDoc) == "Uses Thing and x."
    assert plain_title(NoDoc) == "No doc"
