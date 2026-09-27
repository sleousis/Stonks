"""BL-49: the ranker scores through a point-in-time lake, and drops names
that are not members of the tick's stored universe on the tick date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from stonks.config import Settings
from stonks.core.interval import Interval
from stonks.production.ranker import Ranker
from stonks.production.settings_builder import build_tick_settings
from stonks.store.pit import PointInTimeLake

AS_OF = date(2026, 3, 2)


class _Recorder:
    applicable_asset_classes = ("equity",)

    def __init__(self) -> None:
        self.seen: list[tuple[str, Any]] = []

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        self.seen.append((ticker, lake))
        return 1.0


@dataclass
class _Handle:
    id: str
    class_path: str = "x:y"


class _Registry:
    def __init__(self, strategies: dict[str, Any]) -> None:
        self._strategies = strategies

    def list_all(self, status: str | None = None) -> list[_Handle]:
        return [_Handle(sid) for sid in self._strategies]

    def load(self, sid: str) -> Any:
        return self._strategies[sid]


class _Lake:
    def __init__(self, members: dict[str, list[str]] | None = None) -> None:
        self.members = members or {}
        self.asked: list[tuple[str, Any, Any]] = []

    def get_asset_classes(self, tickers: list[str]) -> dict[str, str]:
        return dict.fromkeys(tickers, "equity")

    def members_between(self, universe_id: str, start: Any, end: Any) -> list[str]:
        self.asked.append((universe_id, start, end))
        return self.members.get(universe_id, [])


def _ranker(lake: _Lake, strategy: _Recorder, **kw: Any) -> Ranker:
    return Ranker(
        registry=_Registry({"s": strategy}),  # type: ignore[arg-type]
        lake=lake,  # type: ignore[arg-type]
        universe=["A.US", "B.US", "C.US"],
        **kw,
    )


def test_strategies_get_a_point_in_time_view_of_the_tick_date():
    strategy = _Recorder()
    signals = _ranker(_Lake(), strategy).score(AS_OF)
    lakes = {id(lake) for _, lake in strategy.seen}
    assert len(lakes) == 1
    view = strategy.seen[0][1]
    assert isinstance(view, PointInTimeLake)
    assert view.as_of == datetime(2026, 3, 2)
    assert view.decision_interval == Interval.DAY_1
    # the view lives as long as the signals (wrappers recall it in decide)
    assert signals.lake_view is view


def test_a_stored_universe_drops_names_that_are_not_members_on_the_tick_date():
    lake = _Lake({"idx": ["A.US", "C.US"]})
    strategy = _Recorder()
    signals = _ranker(lake, strategy, universe_id="idx").score(AS_OF)
    assert sorted(signals.scores["s"]) == ["A.US", "C.US"]
    assert {t for t, _ in strategy.seen} == {"A.US", "C.US"}
    assert lake.asked == [("idx", AS_OF, AS_OF)]
    assert signals.universe == ("A.US", "C.US")


def test_without_a_universe_id_every_name_is_scored():
    strategy = _Recorder()
    signals = _ranker(_Lake(), strategy).score(AS_OF)
    assert sorted(signals.scores["s"]) == ["A.US", "B.US", "C.US"]


def test_the_tick_settings_carry_the_universe_id_only_for_the_full_tick():
    settings = Settings.model_validate({"production": {"universe": "sp500"}})
    assert build_tick_settings(settings, ["A.US"]).universe_id == "sp500"
    # explicit tickers (a scoped tick) are traded as given
    assert build_tick_settings(settings, ["A.US"], scoped=True).universe_id is None
    listed = Settings.model_validate({"production": {"universe": ["A.US"]}})
    assert build_tick_settings(listed, ["A.US"]).universe_id is None
