# Principles

These are the lessons from about 60 trading books (roadmap Phase 7) that should change how Stonks works. Each one is a rule you can test. For each rule the list gives the reason and its source, then how Stonks enforces it: either the code that does it today, or the backlog item that will (`BL-xx`, see `docs/research/book-lessons.md`).

When a change breaks one of these rules, the change is wrong or the rule needs a written amendment here. Nothing sits in between.

## 1. Research discipline and overfitting

**P1. Every strategy states its hypothesis before its first lab run.**
Why: a backtest can't tell a real mechanism from a lucky fit. If you can't say who loses money to you and why, you are probably the one losing it (Harris; López de Prado, *Causal Factor Investing*; Chan, *Algorithmic Trading*).
Enforced: BL-26 adds a `hypothesis` card to every strategy. BL-04 stores it with the lab run. BL-24 makes promotion warn when the card is empty.

**P2. Every trial is counted, and a Sharpe ratio is never reported without its trial count.**
Why: search a big enough parameter space and a high Sharpe always turns up. Overfitting is what normally happens, not a rare accident (López de Prado, *AFML*; Bailey et al.; Kahneman's "what you see is all there is").
Enforced: today `LabRunner.run` logs the trial count and then throws the history away (`lab/runner.py:62-67`). BL-04 keeps a trial ledger, with a running count for each strategy class. BL-14 uses it.

**P3. The best of many noisy estimates is biased upward, so it is shrunk before anyone acts on it.**
Why: the winner's curse applies to tuner winners and to the top-ranked pick each tick alike (Kahneman; Bailey and López de Prado, deflated Sharpe).
Enforced: BL-14 (deflated Sharpe) and BL-15 (probability of backtest overfitting) for tuning. BL-08 turns each strategy's scores into z-scores before they are compared.

**P4. Prefer few parameters, and parameter plateaus to sharp peaks.**
Why: a real effect survives nearby parameter values. A lone spike is fitted noise (Kaufman; Carver; Davey; Ehlers's robustness ratio).
Enforced: BL-19 (plateau and cross-instrument tests).

**P5. Test the signal before building a strategy on it.**
Why: the rank IC and the forward returns after an event show cheaply whether a signal predicts anything. Backtests mix the signal up with sizing and costs (Grinold and Kahn; Tulchinsky; Jansen; Grimes's event studies).
Enforced: BL-33 (IC analysis) and BL-34 (event-study test).

**P6. The whole search process must beat the best it can find in noise.**
Why: a tuner run on random data still finds a best trial. The real result must clearly beat that bar (Woodriff, in Schwager's *Hedge Fund Market Wizards*).
Enforced: today the MCPT with re-tuning (`lab/survival/permutation.py`) is a partial check. BL-35 adds the vs-random test.

## 2. Validation

**P7. A test is out of sample only if the tuner never saw its data.**
Why: tests that backtest the tuned window count the fit as evidence (López de Prado).
Enforced: `oos`, `walk_forward` and OOS-mode MCPT score only data the tuner never saw. `perturbation`, `runs_test` and `period_stability` score the validation window by default (BL-21, `lab.dataset.scoring_window`); `window = "full"` through a lab run's `test_options` is an explicit opt-out. The lab runner hands the suite the dataset embargoed for the fitted strategy (`lab/runner.py`, `suite_dataset`).

**P8. Pass/fail gates are statistical, not fixed thresholds.**
Why: six months at Sharpe 0.5 can't be told apart from zero. A gate has to account for sample length, skew, fat tails and autocorrelation (Bailey and López de Prado, PSR and MinTRL; Lo 2002; Carver).
Enforced: today `OutOfSampleTest` passes at a flat Sharpe of 0.5 (`lab/survival/oos.py:12,26`). BL-16 switches it to PSR ≥ 0.95 with a minimum trade count.

**P9. Train and test windows are separated by an embargo at least as long as the label horizon.**
Why: overlapping labels and serially correlated features leak across a boundary with no gap (López de Prado, *AFML* ch. 7; Chan, *Machine Trading*).
Enforced: `LabDataset.embargo_bars` skips that many trading bars between the train and validation windows (`[lab] embargo_bars`, `stonks lab run --embargo-bars`, the API's `embargo_bars`), raised per strategy to its `label_horizon_bars` (`LabDataset.for_strategy`). Walk-forward folds and walk-forward MCPT use the same embargo (BL-20).

**P10. Walk-forward is the default evidence for promotion.**
Why: optimising over one split is close to worthless. Only the stitched out-of-sample segments count (Davey; Kaufman).
Enforced: the `promotion` preset, the default suite of every registering lab run (API, MCP and CLI), runs `walk_forward`. It requires walk-forward efficiency ≥ 0.5 (`[lab.walk_forward] min_wfe`) and hands its stitched out-of-sample segments to `mc_trades` (BL-20). The go-live gate's `promotion_preset` check wants a stored report for every test of that preset.

**P11. Judge strategies on trades, not bars.**
Why: trade-level win rate, payoff, expectancy and the order of trades decide survival. A profit factor computed from per-bar returns is a different number (Davey; Ehlers and Way).
Enforced: today there is no trade list, and the profit factor is per-bar (`backtest/report.py:43-57,107-114`). BL-02 adds the trade ledger. BL-16 requires at least 20 trades. BL-17 adds Monte Carlo over the trade sequence.

**P12. No look-ahead, ever.**
Why: one leaked bar or one early statement invalidates the whole result (McKinney; Graham and Dodd, via Gray and Carlisle's point-in-time rules; Hamilton, who warns that smoothed regime probabilities use future data).
Enforced: today fills happen at the next bar's open (`backtest/engine.py:11-24`), statements are read by `filing_date`, and macro data carries publication lags. BL-49 adds a point-in-time lake proxy, plus a test that plants a future bar for every catalogued strategy.

**P13. Signals and returns use split- and dividend-adjusted prices.**
Why: a 4:1 split looks like a 75% crash and ignored dividends understate total return, so both corrupt signals and equity curves (Chan; Clenow; Wilcox and Crittenden).
Enforced: today the engine and the bar cache read raw `close` (`backtest/engine.py:151`, `strategies/_common.py:101`). Roadmap 8.6 is fixing this now (BL-01, in progress).

**P14. Universes are point in time, delisted names included.**
Why: a universe of names that are alive today inflates every cross-sectional backtest (Malkiel; Clenow; Covel).
Enforced: BL-37 (membership table and survivorship warning) and BL-49 (engine wiring).

## 3. Benchmarking

**P15. Every result is shown next to buy-and-hold of a benchmark and of the equal-weight universe.**
Why: what you gave up is part of the result (Hazlitt's seen and unseen; Bogle; Malkiel).
Enforced: today no report shows a benchmark (`backtest/report.py:43-57`). BL-22 adds one.

**P16. We pay for alpha, not beta.**
Why: a "momentum" strategy that is really long the bull market passes an absolute gate and fails in the next bear market. Active return and the information ratio are what count (Grinold and Kahn; Chan, *Machine Trading*).
Enforced: BL-22 adds a `benchmark_relative` survival test that reports beta, alpha, its t-stat and the IR.

**P17. A new strategy must bring something the pool lacks.**
Why: many weak, uncorrelated streams beat one strong one. A candidate that correlates at 0.7 or more with an existing strategy adds cost and no breadth (Tulchinsky; Meucci; Dalio, via Schwager).
Enforced: BL-47 (correlation-to-pool check) and BL-12 (per-strategy attribution).

## 4. Costs and execution realism

**P18. Costs are on by default in the lab, the tick and the API.**
Why: a strategy that dies once costs are counted was never a strategy (Chan; Bogle; Carver).
Enforced: today `[backtest.costs]` is all zeros (`config/default.toml`), and the tick builds a flat-slippage broker (`production/tick.py:439-454`). Roadmap 8.2 is putting the cost model into the tick now. BL-13 then makes realistic costs the default.

**P19. A strategy must survive twice the modelled costs, and costs may eat at most a third of the pre-cost Sharpe.**
Why: cost estimates are uncertain, and Carver's "speed limit" caps turnover (Chan; Carver; Wilmott).
Enforced: BL-18 (cost-stress and cost-budget test).

**P20. Impact grows with order size relative to volume and with volatility, and no fill takes more than a set share of a bar's volume.**
Why: today a backtest can buy 300% of a small cap's daily volume at one open (Kissell; Harris; Zipline's volume limit).
Enforced: today impact is `impact_bps·sqrt(q/volume)` with no volatility term and no participation cap (`backtest/costs.py:157-164`). BL-30 and BL-31 fix this.

**P21. The backtest fills orders the way we trade live.**
Why: a backtest filled at the open and a live order filled at some intraday price measure different things (Johnson).
Enforced: today the backtest fills at the next open, but the simulated tick fills at the latest close (`production/tick.py:450-456`). Roadmap 8.2 (in progress) brings cost-model parity. BL-32 records any remaining convention gap in TCA. Alpaca auction orders are deferred because the owner does not want Alpaca yet.

**P22. Every order records what it was supposed to cost and what it did cost.**
Why: you can't calibrate a cost model you never measure (Kissell; Bacidore; Perold's implementation shortfall).
Enforced: BL-32 (decision price, arrival price, implementation shortfall, `stonks tca`).

**P23. Turnover is a choice: small changes to a target position do not trade.**
Why: churning at the edge of a ranking bleeds costs, and most of the money is made by sitting (Carver's buffering; Gârleanu and Pedersen, via Velu et al.; Lefevre).
Enforced: BL-08 (`orders_from_targets`, with a 10% buffer).

## 5. Sizing and risk

**P24. Size by risk, not by cash.**
Why: the same cash fraction is a small bet in a bond ETF and a huge one in an altcoin (Carver; Elder; Clenow).
Enforced: today every example sizes with `cash·allocation/price` (`strategies/examples/momentum.py:104-105`). BL-08 adds volatility-targeted and inverse-volatility constructors. BL-27 adds a per-position risk budget.

**P25. No single position risks more than a fixed share of equity (default 2%, measured to the stop or as one-day 99% VaR).**
Why: position size, not the entry, decides ruin (Elder's 2% rule; Vince).
Enforced: BL-27.

**P26. Never above half-Kelly, and never above 1.0 gross in a cash account.**
Why: full Kelly assumes you know the mean. You don't, and to the right of the optimum growth collapses (Chan; Thorp; Sinclair; Vince).
Enforced: today the broker is long-only, `SimulatedBroker` scales buys to available cash, and there is no margin. BL-08 caps gross exposure at 1.0.

**P27. Size comes down automatically in drawdowns and recovers with hysteresis.**
Why: people freeze or double down after losses. The rule has to decide instead (Benedict, via Schwager; Ghosh and Donadio; Coates).
Enforced: BL-27 (drawdown scaling, 5/10/15% → 1.0/0.5/0.25).

**P28. Automated risk rules only reduce exposure.**
Why: a safety layer that can add risk isn't one (Narang's risk model; Part 7 of the Axon series).
Enforced: today the `production/risk.py` docstring and its tests. BL-11 turns this into a property test that runs over every registered rule.

**P29. The future worst drawdown will exceed the historical one.**
Why: the largest historical loss is a lower bound (Vince; Davey's Monte Carlo).
Enforced: BL-17 stores the 95th-percentile Monte Carlo drawdown. BL-25 and BL-29 use it.

## 6. Portfolio construction

**P30. The forecast is separate from the position.**
Why: when a strategy says what it expects and one shared framework turns that into positions, strategies become comparable and combinable (Carver; Narang's five modules; QuantConnect LEAN).
Enforced: today alpha and sizing are fused in each strategy's `decide`. BL-08 adds the `PortfolioConstructor` seam, and BL-12 wires it into the tick and the backtest.

**P31. Strategies are combined, never picked winner-take-all.**
Why: IR ≈ IC·√breadth. One winner per tick means a breadth of about one, and the book churns whenever the winner changes (Grinold and Kahn; Carver; Dalio).
Enforced: today the tick trades only the owner of `ranked[0]` (`production/tick.py:237-244`). BL-12 fixes this.

**P32. Scores go on one scale before they are compared.**
Why: raw `estimate_return` units differ by strategy. `BuyAndHold` returns 1.0 (`strategies/examples/buy_and_hold.py:44`) and beats any realistic forecast (Grinold and Kahn: α = σ·IC·z; Carver's forecast scaling, mean |f| = 10, capped at 20).
Enforced: BL-08 (`portfolio/signals.py`).

**P33. Count bets, not positions.**
Why: ten correlated momentum names are about one bet (Meucci's effective number of bets; López de Prado's HRP).
Enforced: BL-44.

## 7. Regimes and crises

**P34. Check the tide before the stock: regime filters gate new buys.**
Why: being right on a stock and wrong on the market loses money (Lefevre; Clenow's index filter; Kindleberger).
Enforced: today `MacroRegimeFilter` and `FeatureRegimeFilter` (`strategies/macro_regime.py`, `strategies/feature_regime.py`). BL-42 adds a composite k-of-n filter (price trend, realised volatility, yield curve, macro).

**P35. Every strategy must be seen through at least one crisis before promotion, where data allows.**
Why: a backtest that skips a crash is lying, and risk models fail exactly when they're needed (Kindleberger; Danielsson).
Enforced: BL-48 (crisis windows and stress simulation). The EODHD free tier's one-year limit makes this a data requirement (BL-37 preflight).

**P36. Diversification is not a crash hedge: correlations go to one in panics.**
Why: contagion (Kindleberger; Harris on liquidity vanishing).
Enforced: BL-27 caps portfolio volatility with a correlation-shock bound. BL-28 adds the circuit breaker.

**P37. Edges belong to an asset class and a horizon.**
Why: 50-day breakouts have a measured negative edge in single stocks and a positive one in futures, and one-month stock returns reverse while 3-12-month returns trend (Grimes; Chan; Gray and Vogel).
Enforced: today the defaults contradict this (20-day momentum, `momentum.py:36-41`; Donchian defaults to `AAPL.US`). BL-43 fixes the defaults. BL-34 checks each strategy's edge per asset class.

## 8. Operations and pre-commitment

**P38. The quit rule is written down before going live.**
Why: loss aversion and sunk cost make every stop-trading decision late (Davey; Kahneman).
Enforced: BL-29. Live drawdown beyond 1.5× the backtest drawdown or the Monte Carlo p95 raises an alert, with optional automatic demotion.

**P39. Incubate before real money.**
Why: real-time results are the only evidence you can't overfit (Davey).
Enforced: today shadow mode and `stonks golive check`, at 20 days and 5 trades (`config.py:173,180`). BL-25 raises this to max(63 days, MinTRL) and 20 trades, and adds a check that live results fall inside the Monte Carlo band.

**P40. Kill switches act on their own, only reduce risk, and are re-enabled only by a logged human action.**
Why: crypto trades while the operator sleeps, and people override systems at the worst moment (Elder's 6% rule; Carver; Part 7 of the Axon series).
Enforced: BL-28.

**P41. No human change to status, risk or orders happens without a reason on record.**
Why: decisions made by mood are noise (Kahneman, *Noise*; Tendler's mistake log).
Enforced: today status changes carry no reason and no audit row (`registry/store.py:104-112`, `app/strategies.py:108-115`). BL-24 adds both.

**P42. The machine keeps the trade journal.**
Why: a generated review beats looking at the equity curve and reacting to the latest move (Elder; Steenbarger).
Enforced: BL-32 (decision context on every order, `stonks journal`).

## 9. Engineering and scalability

**P43. A new concept arrives as an abstraction behind a seam with a registry, never as copy-paste in each strategy.**
Why: Narang's five modules and LEAN's pluggable models scale because each concern has one place. Duplicated sizing and exit logic across 20 strategies does not.
Enforced: today `Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`, `CostModel`, `DataSource` and the strategy catalog (`lab/catalog.py`). BL-08 adds constructors, BL-10 survival tests, BL-11 risk rules, and BL-42 regime conditions, each with its own registry.

**P44. Every list has one source of truth.**
Why: two lists drift apart. Today the CLI keeps its own survival-test list (`cli.py:947`) next to the API's `Literal` (`app/lab.py:43`).
Enforced: BL-10.

**P45. Parallelise at the coarsest independent grain, with deterministic seeds for each task and one DuckDB connection per worker process.**
Why: tuning trials, permutations, walk-forward folds, noise lakes and per-ticker work are embarrassingly parallel. Results must not depend on the worker count (owner requirement; Slatkin).
Enforced: today everything runs serially, apart from the API's thread pool (`app/jobs.py:285`). Roadmap 6.6 is adding the `lab/parallel.py` process-pool helper for MCPT now. BL-07 extends that same helper to tuners, folds and noise lakes. There is no second pool.

**P46. Every lab result can be reproduced.**
Why: a verdict you can't rerun can't be trusted or debugged (Slatkin; Strimpel; López de Prado).
Enforced: today `RandomTuner` is seeded (`lab/tuning/random.py:19-25`). BL-06 records the git SHA, a config hash, a data fingerprint and the seeds.

**P47. Third-party libraries sit behind our seams, and statistics use numpy and scipy.**
Why: vendor types must not leak (CLAUDE.md). The ADIA Lab reference code shows that PSR, DSR and PBO need nothing heavier.
Enforced: today CLAUDE.md's working rules. `statsmodels`, `arch` and `cvxpy` get wrappers when BL-44, BL-46 and BL-48 land.

**P48. Tests come first and are hermetic, and every invariant gets a property test.**
Why: invariants such as cost monotonicity, cash never going negative and rules never adding exposure should be checked for all inputs, not for three examples (Slatkin).
Enforced: today TDD with `FakeDataSource`. BL-49 adds Hypothesis and pyright.
