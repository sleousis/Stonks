# Strategies

Every strategy the lab can name, grouped by family. The list comes from `stonks.lab.catalog`: each public class in `strategies/examples/` plus three wrappers, 28 in all. All are long-only; short signals mean "flat".

## Run one

```bash
uv run stonks lab run momentum --start 2023-01-01 --end 2025-01-01 --tickers AAPL.US,MSFT.US
uv run stonks lab run quant_momentum --start 2020-01-01 --end 2025-01-01 --preset promotion --register
```

`--params '{"lookback_days": 126}'` sets parameters. Wrappers take the inner strategy in their params (`inner_class_path`, `inner_params`).

## Reference

| Id | What it does |
|----|--------------|
| `buy_and_hold` | Buys one ticker at a set allocation and holds it. |
| `momentum` | Holds the ticker with the best trailing return; sells what drops out of the ranking. |
| `donchian_breakout` | Long above the prior N-bar high, flat below the prior N-bar low. |
| `trendline_breakout` | Long when the close breaks a fitted, sloped resistance line; flat below support. |

## Fundamentals

| Id | What it does |
|----|--------------|
| `quality_value` | Point-in-time value (earnings and FCF yield) plus quality (ROE, margin, leverage); holds the top K. |

## Book strategies ([details](book-strategies.md))

| Id | What it does |
|----|--------------|
| `quant_momentum` | Gray and Vogel: 12-2 momentum, top decile, frog-in-the-pan filter, quarterly rebalance. |
| `stocks_on_the_move` | Clenow: regression slope times R², moving-average and gap filters, ATR-parity sizing. |
| `quant_value` | Gray and Carlisle: EBIT/TEV value after forensic screens, with quality; Piotroski and magic-formula modes. |

## Trend following ([details](book-strategies.md#cross-asset-trend-following-bl-40))

| Id | What it does |
|----|--------------|
| `ewmac_trend` | Carver's EWMAC forecasts at several speeds, per instrument. Equity, crypto, commodity. |
| `tsmom` | Time-series momentum: each instrument against its own past return. Equity, crypto, commodity. |
| `ath_trend` | Buys weekly closes at all-time highs, exits on a wide ATR trailing stop. Equity and crypto. |

## neurotrader888 indicators ([details](nt888-indicators.md))

| Id | What it does |
|----|--------------|
| `volatility_hawkes` | Breakout when a Hawkes-process volatility measure spikes. |
| `visibility_graph_path` | Signal from the path length of a price visibility graph. |
| `vsa` | Volume spread analysis: volume against range. |
| `market_profile_sr` | Trades penetrations of market-profile support and resistance. |
| `intramarket_difference` | Trades one market on its trend relative to another. |
| `ma_crossover` | Long while the fast moving average is above the slow one. |

## Chart patterns ([details](nt888-patterns.md))

| Id | What it does |
|----|--------------|
| `head_shoulders` | Long the breakout of an inverse head and shoulders. |
| `flag_pennant` | Long the breakout of a bull flag or pennant. |
| `harmonic_xabcd` | Long bullish harmonic XABCD patterns. |
| `market_structure_break` | Long a break of hierarchical market structure. |

## Machine learning ([details](nt888-filters-ml.md))

| Id | What it does |
|----|--------------|
| `pip_miner` | Mines perceptually important point patterns and trades the best cluster. |
| `trendline_meta_label` | Trendline breakouts filtered by a random-forest meta-label model. |
| `rsi_pca` | PCA over many RSI periods and a linear read-out with quantile thresholds (see its module docstring). |

## DeFi ([details](nt888-tvl.md))

| Id | What it does |
|----|--------------|
| `tvl_deviation` | Mean reversion of a crypto price against its DeFi-TVL-implied price. Needs `stonks ingest tvl`. |

## Wrappers

A wrapper gates an inner strategy. It takes the inner strategy's asset classes.

| Id | What it does |
|----|--------------|
| `macro_regime_filter` | Goes flat when a point-in-time macro series (from `macro_indicators`) says risk off. |
| `feature_regime_filter` | Goes flat per ticker when a complexity feature (permutation entropy, reversibility, runs) says risk off. [Details](nt888-filters-ml.md). |
| `last_trade_filter` | Takes the inner strategy's entry only after its previous trade lost (or won). [Details](nt888-filters-ml.md). |

## Not in the catalog

- **`TrailingStopWrapper`** (`strategies/trailing_stop.py`): a volatility-scaled trailing stop around any strategy. Used in code; `stonks lab run` cannot name it yet. [Details](book-strategies.md#trailingstopwrapper).
- **`RuleStrategy`** (`strategies/rule_based.py`): a strategy defined by a JSON spec (indicators, entry and exit rules, sizing, stops). Built, backtested and registered from the Strategy Studio page in the console.

## Portfolio constructors

A constructor turns the strategies' signals into target weights (`portfolio/`). Registered ones:

| Name | Sizing |
|------|--------|
| `single_winner` (default) | The strategy with the best pick takes the book and its own `decide` makes the orders. |
| `equal_weight_top_n` | Equal weight across the N best positive scores. |
| `inverse_vol` | Weight proportional to 1/volatility across the top N, scaled to a gross limit. |
| `vol_target` | Carver's position sizing from forecasts to a volatility target. |
| `atr_parity` | Equal daily risk per position: weight = risk factor x price / ATR. |

A portfolio picks its constructor in `portfolios.construction_json`; a backtest in `BacktestConfig.construction`. A global `[production.construction]` table is not read from the config file yet.
