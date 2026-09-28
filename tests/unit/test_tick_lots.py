"""Which lot rule a tick book trades with (roadmap 23.1, P21)."""

from __future__ import annotations

from stonks.config import Settings
from stonks.portfolio.lots import LotSettings
from stonks.production.settings_builder import build_tick_settings
from stonks.production.tick import TickSettings


def test_paper_books_follow_backtest_lots():
    settings = TickSettings(universe=["X.US"], lots=LotSettings(profile="whole_shares"))
    assert settings.lot_rule(external=False).profile.name == "whole_shares"
    assert TickSettings(universe=["X.US"]).lot_rule(external=False).profile.name == "fractional"


def test_live_books_follow_their_broker():
    ibkr = TickSettings(universe=["X.US"], broker_kind="ibkr")
    assert ibkr.lot_rule(external=True).profile.name == "whole_shares"
    alpaca = TickSettings(universe=["X.US"], broker_kind="alpaca")
    assert alpaca.lot_rule(external=True).profile.name == "fractional"


def test_live_books_keep_ticker_lot_sizes():
    settings = TickSettings(
        universe=["X.US"], broker_kind="ibkr", lots=LotSettings(lot_sizes={"X.US": 100})
    )
    assert settings.lot_rule(external=True).lot("X.US", "equity") == 100.0


def test_builder_passes_backtest_lots():
    settings = Settings(backtest={"lots": {"profile": "whole"}})
    assert build_tick_settings(settings, ["X.US"]).lots.profile == "whole"
