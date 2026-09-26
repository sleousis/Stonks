"""Lab request options the API accepts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.app.lab import McptOptions


@pytest.mark.parametrize(
    "metric", ["profit_factor", "bar_profit_factor", "sharpe", "final_return", "cagr"]
)
def test_mcpt_metric_names(metric):
    assert McptOptions(metric=metric).metric == metric


def test_unknown_mcpt_metric_is_refused():
    with pytest.raises(ValidationError):
        McptOptions(metric="sortino")
