# Book lessons: merged backlog

This is the synthesis of five book-research reports: systems, sizing and risk; markets, fundamentals and portfolios; statistics, time series and ML; execution, volatility and engineering; and the Axon 100-book series. It covers about 60 books. The durable rules are in `docs/principles.md`. This file is the backlog those rules generate: 49 items, deduplicated, each checked against the code on `feat/roadmap` at commit `7207f72`.

How to read an item:
- **Sources**: the books or authors that argue for it.
- **Problem**: what Stonks does today, with file:line references checked at `7207f72`.
- **Spec**: formulas, parameters and defaults. Values marked "verify" come from memory of a book and must be checked before they become hard-coded defaults.
- **Seam**: the abstraction the item belongs behind.
- **Owns**: the files the implementing agent may edit.
- **Parallel**: where all 32 cores help.
- **Tests**: the TDD list to write first.
- **Size**: impact (H/M/L) and effort (S/M/L).
- **Deps**: items that must land first.

Constraints for every item:
- Long-only, with the simulated broker by default.
- No Alpaca work, and no options data.
- `CLAUDE.md` rules: TDD, hermetic tests, vendor-agnostic schemas, and third-party libraries wrapped behind a seam.

## 0. Already in progress elsewhere (not scheduled here)

Other agents are building these now. Phase 9 builds on them and does not duplicate them.

| Roadmap WP | What | Relation to this backlog |
|---|---|---|
| 8.6 | Split- and dividend-adjusted prices for signals, split ratios applied to holdings, dividends credited as cash in backtests | This is **BL-01**. Every later item assumes it has landed. |
| 8.2 | The `[backtest.costs]` model in the tick and in Studio lab runs, giving tick fills cost-model parity with backtests | This is the wiring half of **BL-13**. BL-13 keeps only the "realistic by default" config change. |
| 8.5 | Alpaca records the order as pending before submitting | Out of scope, because Alpaca work is deferred. |
| 6.6 | RSI-PCA `hold_bars`, a trade-level runs test, a walk-forward permutation test, and the **`lab/parallel.py` process-pool helper** used by MCPT | BL-07 **extends** this helper. There is no second pool. BL-02's trade ledger should replace any private trade-pairing code the runs test adds. |
| 6.7 | DefiLlama TVL source, `defi_tvl` table and TVL strategy | None. |
| 5.2 | Angular trader console in `web/` | The UI surfaces for BL-22, BL-32 and BL-47 belong there later. |

## 1. Cross-cutting design rules for Phase 9

These apply to every package below. They are how the principles P43-P48 turn into code.

1. **Registries, not lists.** Every new plug-in kind is found automatically by its module, the way `lab/catalog.py` finds strategies. That covers survival tests (BL-10), risk rules (BL-11), portfolio constructors (BL-08), regime conditions (BL-42) and post-tick hooks (BL-12). Adding a new test, rule or constructor means adding one new file and editing no shared list.
2. **Optional capabilities are duck-typed hooks,** as with `bind_tuning` today (`lab/runner.py:71-74`). Examples are `score_universe`, `bind_run`, `label_horizon_bars` and `required_history_bars`. `core/protocols.Strategy` stays minimal, and callers use `getattr(obj, name, default)`.
3. **Integration hotspots are wired in the step between waves, not inside packages.** These are `config.py`, `config/default.toml`, `cli.py`, `api/routers/*`, `mcp/server.py`, `lab/catalog.py` (`_WRAPPERS`), `pyproject.toml` and `uv.lock` (new dependencies), `tests/conftest.py`, and the docs. Each package defines its own pydantic settings model and service function in its own module. The integration step (roadmap "merge, review, fix") adds the config field, the CLI flag and the API route, usually under 10 lines per package.
4. **Migration numbers are pre-assigned:**
   - SQLite: `006_lab_trials` (W1.2), `007_status_changes` (W1.6), `008_position_attribution` (W2.1), `009_risk_halts` (W3.2), `010_tca` (W3.4), `011_risk_snapshots` (W5.4).
   - DuckDB: `011_statement_flags`, `012_universe_membership` (both W3.6).
   - If 6.7 or another in-progress package takes a number first, shift these up in order, and do it in the integration step.
5. **Parallelism contract (BL-07):**
   - One pool: `lab/parallel.py`.
   - Workers use the `spawn` start method, so the code runs on Windows.
   - Each worker process opens exactly one DuckDB connection: `read_only=True`, pointed at a universe-scoped snapshot file.
   - Each task gets its seed from `numpy.random.SeedSequence(root_seed).spawn(n)[i]`, and results are returned in task order. Output therefore does not depend on the worker count.
   - BLAS/OpenMP threads are pinned to 1 in each worker.
   - Nested pools are forbidden. A task that tunes runs its inner tuner in-process.
   - `max_workers=1` takes the same code path without a pool, for tests and debugging.

## 2. Backlog items

### Data correctness and backtest foundations

#### BL-01 Adjusted prices and corporate actions (in progress, roadmap 8.6)
- **Sources:** Chan (*Quantitative Trading*), Clenow (*Stocks on the Move*), Wilcox & Crittenden (via Covel), the execution report's cross-cutting finding.
- **Problem:** `backtest/engine.py:151` selects raw `open, close`. `strategies/_common.py:101` builds `closes` from raw `close`. `stock_splits` (`store/lake.py:1170`) and `dividends` (`store/lake.py:1100`) are never read by the engine.
- **Status:** being built under 8.6. Acceptance test that every later item relies on: a synthetic 4:1 split leaves the buy-and-hold equity unchanged, and a dividend lands as cash.

#### BL-02 Round-trip trade ledger
- **Sources:** Davey (both books), Ehlers & Way, Elder, Pik & Ghosh, Clenow (*Trading Evolved*).
- **Problem:** `BacktestReport` (`backtest/report.py:43-57`) has no trades. `profit_factor` is computed over per-bar returns (`report.py:107-114`). The broker's `_fills_order` (`backtest/simulated_broker.py:128`) is never read. `lab/survival/oos.py:26` passes a strategy that made a single trade.
- **Spec:**
  - `backtest/trades.py` defines `RoundTrip(ticker, strategy_key, entry_ts, exit_ts, qty, entry_px, exit_px, pnl, return_pct, bars_held, fees, slippage_cost, mae_pct, mfe_pct, is_open)`.
  - Fills are paired FIFO per (`client_id` strategy prefix, ticker), using the `"<index>:"` prefix the engine adds at `engine.py:216`. Partial exits split lots. Lots still open at the end are marked at the last close and flagged `is_open`.
  - `TradeStats` fields:
    - `n_trades` (closed trades only), `win_rate`, `avg_win`, `avg_loss` (negative), `payoff_ratio = avg_win/|avg_loss|`;
    - `expectancy = p·avg_win + (1−p)·avg_loss`;
    - `trade_profit_factor = Σwins/|Σlosses|`;
    - `avg_bars_held`, `exposure` (the share of bars with any position);
    - `turnover_annual = Σ|traded notional| / mean equity / years`;
    - `costs_paid = Σ(fees + |fill − reference price|·qty)`.
  - `SimulatedBroker` exposes a read-only `fills` tuple and a `reference_price(client_id)`, the pre-cost price, kept in an internal map. `core.types.Fill` is not changed here.
  - `lab/backtesting.run_backtest` attaches the ledger: `report = with_trades(report, broker.fills, bars)`. The engine stays untouched. MAE and MFE come from the ticker's bars over [entry, exit] and are `None` when the bars aren't passed.
  - The per-bar figure is renamed `bar_profit_factor`. `profit_factor` stays as a deprecated alias for one release.
- **Seam:** pure functions over `Fill` sequences. Every caller (lab, API backtests, reporting) reads the report fields.
- **Owns:** `backtest/trades.py` (new), `backtest/report.py`, `backtest/simulated_broker.py`, `lab/backtesting.py`.
- **Parallel:** none needed. The ledger is O(fills).
- **Tests:**
  - FIFO with scale-in and partial exit;
  - an open lot flagged and marked;
  - hand-computed expectancy and profit factor;
  - turnover on a two-trade fixture;
  - zero trades gives `n_trades == 0` and no division errors;
  - two strategy instances in one backtest keep separate ledgers.
- **Size:** H / M.
- **Deps:** BL-01.

#### BL-03 Richer, correct performance metrics
- **Sources:** Danielsson; Carver (lower-tail ratio); Tulchinsky (fitness); Bogle (cost drag); Slatkin (the ddof point); Ghosh & Donadio (Sortino).
- **Problem:** Sharpe uses the population variance (`report.py:84`). There is no Sortino, Calmar, drawdown duration, skew, kurtosis, autocorrelation or ES, so PSR can't be computed from a report.
- **Spec:** `backtest/metrics.py` holds pure functions over the per-bar return series:
  - Sharpe with ddof=1 and optional `risk_free_rate` (default 0);
  - `sortino = mean(r)/sqrt(mean(min(r,0)²))·√ppy`;
  - `calmar = cagr/|max_dd|`;
  - `ulcer = sqrt(mean(dd_t²))`, and `upi = cagr/ulcer`;
  - `max_dd_duration_bars`;
  - historical `var_95` and `es_95` per bar;
  - `skew`, and non-excess `kurtosis` (normal = 3, the convention PSR needs);
  - `autocorr_1`;
  - `lower_tail_ratio = (p1/p30 of demeaned r)/(2.326/0.524)`;
  - `fitness = sharpe·sqrt(|cagr|/max(turnover_daily, 0.125))`.

  `BacktestReport` gains these fields and a `returns` property derived from `equity_curve`. Golden-value tests that pinned the old Sharpe are updated in the same change.
- **Seam:** same module as BL-02's stats. Reporting and survival tests read the fields and never recompute them.
- **Owns:** `backtest/metrics.py` (new), plus `backtest/report.py` (shared with BL-02 in the same package).
- **Parallel:** none.
- **Tests:** known series for each metric; Sortino with no negative returns; the ddof=1 Sharpe on a constant series is 0; kurtosis is about 3 on a large normal sample with a fixed seed.
- **Size:** M / S.
- **Deps:** none.

#### BL-04 Trial ledger and pre-registration
- **Sources:** López de Prado (*AFML*, *MLAM*), Bailey et al., Kahneman, Jansen (3rd edition, RAS protocol), Woodriff (via Schwager).
- **Problem:** `lab/runner.py:62-67` logs `trials=len(tuned.history)` and drops the history. `LabRunResult` (`runner.py:23-30`) has no history. Nothing counts trials across runs of a strategy class. There is no place for a hypothesis.
- **Spec:**
  - SQLite migration `006_lab_trials.sql`:
    - `lab_runs(id TEXT PK /*ulid*/, strategy_class, hypothesis, premortem, tuner, objective, budget, seed, dataset_json, manifest_json, started_at, finished_at, verdict)`;
    - `lab_trials(run_id FK, trial_index, params_json, score REAL, n_bars INT, status TEXT, PRIMARY KEY(run_id, trial_index))`.
  - Per-trial per-bar returns are written as a float32 matrix, T×N with a date index, to `data/artifacts/_trials/<run_id>.npz`, not into SQLite.
  - `lab/trials.py` defines `TrialLedger` with `record_run(...)`, `trial_matrix(run_id)` and `n_trials(strategy_class)`. The last one is the cumulative count, because repeated lab runs are trials too.
  - `LabRunner.run(..., hypothesis=None, premortem=None)` records every run. `LabRunResult` gains `run_id`, `history`, `n_trials_run` and `n_trials_class`.
  - Survival tests with a `bind_run(ctx: LabRunContext)` hook (the dataclass lives in `lab/trials.py`) get `ctx.setup`, `ctx.ledger`, `ctx.run_id` and `ctx.trial_matrix`. That is how BL-14 and BL-15 see the trials.
  - The per-trial returns come from `TunerResult.trials` when the tuner provides them (BL-07). Otherwise only the scores are stored.
  - Registry `meta.json` gains `lab_run_id`, `n_trials_total`, `hypothesis` and `premortem`.
- **Seam:** `TrialLedger`, the one writer. Tests read it through `LabRunContext`.
- **Owns:** `lab/runner.py`, `lab/trials.py` (new), `store/migrations_sqlite/006_lab_trials.sql`. (`registry/artifact.py` is shared with BL-06 in the same package.)
- **Parallel:** none. The writes are batched once per run.
- **Tests:**
  - a run writes N trial rows plus a matrix of shape (T, N);
  - failed trials are stored as NaN with `status='failed'`;
  - two runs of one class give `n_trials == 2N`;
  - `bind_run` is called before `suite.run`;
  - meta.json carries the fields;
  - hermetic: SQLite and a tmp artifacts directory.
- **Size:** H / M.
- **Deps:** none (it gets richer after BL-07).

#### BL-05 Statistics kernel
- **Sources:**
  - Bailey & López de Prado: PSR, DSR, MinTRL.
  - López de Prado, Lipton & Zoonekynd (2025): the null-variance PSR with AR(1) correction.
  - Lo (2002): Sharpe standard error.
  - Politis & Romano: stationary bootstrap.
  - Newey & West: HAC.
  - Bailey et al.: CSCV.
  - Benjamini & Hochberg; Holm.
- **Problem:** there is no statistical inference anywhere, and `scipy` is only a transitive dependency (`pyproject.toml`).
- **Spec:** a new top-level `src/stonks/stats/` package, numpy and scipy only.
  - `sharpe.py`:
    - `sharpe_variance(sr, T, skew, kurt, rho=0) = (a − b·skew·sr + c·(kurt−1)/4·sr²)/T`, with `a = 1+2ρ/(1−ρ)`, `b = 1+ρ/(1−ρ)+ρ²/(1−ρ²)` and `c = 1+2ρ²/(1−ρ²)`;
    - `psr(sr, sr0, T, skew, kurt, rho=0) = Φ((sr−sr0)/sqrt(sharpe_variance(sr0, …)))`, with the variance taken under the null;
    - `min_trl(sr, sr0, skew, kurt, rho, alpha=0.05) = sharpe_variance(sr0,T=1,…)·(z_{1−α}/(sr−sr0))²`, in bars;
    - `expected_max_sharpe(n, var_sr) = sqrt(var_sr)·((1−γ)Φ⁻¹(1−1/n) + γΦ⁻¹(1−1/(n·e)))`, with γ = 0.5772156649;
    - `dsr(...) = psr(sr, expected_max_sharpe(...), ...)`;
    - `sharpe_se_annual(sr, years) = sqrt((1+0.5·sr²)/years)`;
    - `effective_n(corr) = exp(entropy of normalised eigenvalues)`.
  - `bootstrap.py`: `stationary_bootstrap_indices(T, mean_block=20, n, rng)` and `sharpe_ci(returns, alpha=0.05, n_boot=2000, mean_block=20, seed)`.
  - `hac.py`: `newey_west_se(x, lags=None)`, with a default lag of `floor(4·(T/100)^(2/9))`.
  - `pbo.py`: `cscv(M, n_blocks=10, max_combinations=5000, seed) -> PBOResult(pbo, degradation_slope, p_loss, n_combinations)`, following the algorithm in the stats report §1.3. Combinations beyond the cap are sampled with the seed.
  - `multiple_testing.py`: `holm(pvals, alpha)` and `benjamini_hochberg(pvals, q)`.
- **Seam:** a pure library with no Stonks imports, so `backtest/`, `lab/` and `production/` can all use it.
- **Owns:** `src/stonks/stats/*` (new), plus `scipy` added explicitly to `pyproject.toml` (integration step).
- **Parallel:** vectorised numpy. CSCV and the bootstrap work on whole matrices.
- **Tests:**
  - `psr(0.456, 0, 24, −2.448, 10.164) ≈ 0.987` (the reference check value);
  - `expected_max_sharpe` grows with n;
  - DSR equals PSR(0) when n = 1;
  - MinTRL shrinks as SR grows;
  - bootstrap CI coverage around 95% on iid normal data, seeded;
  - Newey-West equals the iid SE when autocorrelation is 0;
  - PBO below 0.1 for a matrix with one dominant true column, about 0.5 for pure noise;
  - BH on a textbook example.
- **Size:** H / M.
- **Deps:** none.

#### BL-06 Reproducibility manifest
- **Sources:** Slatkin, Strimpel, López de Prado (research factory).
- **Problem:** `registry/artifact.py` stores params and reports, but no code version, config or data identity. Only `RandomTuner` is seeded (`lab/tuning/random.py:19-25`).
- **Spec:** `lab/manifest.py` provides `build_manifest(settings, dataset, seeds) -> dict`. It contains:
  - `git_sha` and `git_dirty` (from `git rev-parse`, or `None` when git isn't available);
  - `stonks_version`;
  - `config_hash`: sha256 of the resolved Settings JSON, secrets excluded;
  - `data_fingerprint`: for each universe ticker, `count`, `min(ts)`, `max(ts)` and `hash(list(close ORDER BY timestamp))` over the lab window, computed in DuckDB with one query;
  - `costs`, the `CostModelSettings` in force;
  - `seeds`.

  The manifest is stored in `lab_runs.manifest_json` and in `meta.json`.
- **Seam:** a pure builder. The runner calls it once.
- **Owns:** `lab/manifest.py` (new) and `registry/artifact.py`.
- **Parallel:** none.
- **Tests:** the fingerprint changes when one close changes and is stable otherwise; secrets are absent from the hash input; no git gives `None`.
- **Size:** M / S.
- **Deps:** BL-04 (same package).

#### BL-07 Parallel lab execution (extends 6.6's `lab/parallel.py`)
- **Sources:** the owner's 32-core requirement; Slatkin (`concurrent.futures`); Hilpisch (research throughput).
- **Problem:**
  - The tuners loop serially: `lab/tuning/random.py` in `tune` and `lab/tuning/grid.py` likewise.
  - Walk-forward folds run serially (`lab/survival/walk_forward.py:162`).
  - `DuckDBLake` always opens read-write (`store/lake.py:302`), and a read-write connection locks the file against other processes.
  - 6.6 adds a pool for MCPT only.
- **Spec:**
  - Extend `lab/parallel.py` (do not fork it) with:
    - `ParallelSettings(max_workers=0 → min(32, os.cpu_count()), blas_threads=1)`;
    - `LakeSnapshot`: a universe- and window-scoped DuckDB file built once per lab run in a temp directory with `lab/lake_copy.copy_universe_lake` plus that window's bars. This avoids lock conflicts with the API or ingest and fixes the data behind the run's fingerprint;
    - `run_tasks(fn, tasks, *, snapshot, root_seed, settings) -> list[TaskResult]`, which follows the §1 contract.
  - Tasks are picklable: a strategy class path plus params, and a `DatasetSpec` (snapshot path, universe, windows, interval, costs, embargo).
  - `DuckDBLake(path, read_only=False)` becomes a constructor option.
  - `lab/objectives.py`: every objective gains `evaluate(strategy, dataset) -> TrialOutcome(score, returns: np.ndarray, n_bars)`, and `score` becomes `evaluate(...).score`.
  - `core/protocols.TunerResult` gains `trials: list[TrialOutcome] | None = None`.
  - `GridTuner` and `RandomTuner` generate candidates up front (already deterministic), evaluate them with `run_tasks`, and fill `trials`. A failing task becomes a NaN trial, as today.
  - Cooperative cancellation checks between tasks, so API job cancellation still works.
- **Seam:** `run_tasks` is the only way anything in Stonks runs work across processes. Consumers are BL-15, BL-18 to BL-21, BL-33 to BL-35, BL-45, BL-47 and BL-48.
- **Owns:** `lab/parallel.py` (extend), `lab/tuning/base.py`, `lab/tuning/grid.py`, `lab/tuning/random.py`, `lab/objectives.py`, `core/protocols.py`, `store/lake.py` (the `read_only` option only).
- **Parallel:** this is the item that makes all the others parallel. On 32 cores a 200-trial random search runs about 25× faster, with the snapshot load amortised.
- **Tests:**
  - serial and 2-worker runs give identical `history` and `trials`;
  - results don't change with `max_workers ∈ {1,2,3}`;
  - a worker's lake refuses writes;
  - an in-memory source lake gets materialised to a snapshot file;
  - a task that raises becomes a NaN trial;
  - no subprocess is spawned when `max_workers=1`;
  - `TunerResult.trials` aligns with `history`.
- **Size:** H / M.
- **Deps:** 6.6 merged.

### Portfolio construction and risk seams

#### BL-08 Portfolio-construction seam
- **Sources:** Carver (*Systematic Trading*: forecasts, vol targeting, FDM/IDM, buffering); Grinold & Kahn (α = σ·IC·z, transfer coefficient); Narang (five modules); Boyd; Pfaff; LEAN (via Scarpino et al.); Kahneman (shrink estimates).
- **Problem:**
  - Winner-take-all: `production/tick.py:237-244` trades only the strategy that owns `ranked[0]`.
  - `production/ranker.py:82-94` sorts raw `estimate_return` across strategies whose units are incomparable: `buy_and_hold.py:44` returns 1.0, and `quality_value.py:212` returns `return_scale·score`.
  - Sizing is `cash·allocation/price` in each strategy (`momentum.py:104-105`).
- **Spec:** a new `src/stonks/portfolio/` package.
  - `base.py`:
    - `ConstructionInput(signals: Mapping[strategy_id, Mapping[ticker, float]], strategy_weights, portfolio, prices, vols_annual, asset_classes, as_of)`;
    - an ABC `PortfolioConstructor.target_weights(inp) -> TargetBook(weights: dict[ticker, float], attribution: dict[ticker, dict[strategy_id, float]])`;
    - `@register_constructor(name)` and `get_constructor(name, **settings)`, where an unknown name lists the valid ones.
  - `signals.py`: `normalize(raw, method)`.
    - `"zscore"`: a cross-sectional z-score within each strategy, winsorised at ±3.
    - `"rank"`: percentiles.
    - `"forecast"`: Carver scaling. `f = raw·scalar`, where `scalar = 10/mean|raw|` is estimated in the lab and stored, capped at ±20.
    - `"alpha"`: `α_i = IC_s·σ_i·z_i`, with `IC_s` from the registry (BL-33) and a default of 0.02.
    - A strategy with fewer than 3 names gets a sign-only conviction of 1.0.
    - In long-only mode, negatives are clipped to 0.
  - Constructors (`constructors.py`):
    - `single_winner`: today's behaviour, kept for back-compat and parity tests.
    - `equal_weight_top_n(n=10)`.
    - `inverse_vol(top_n=20)`: `w_i ∝ 1/σ_i`.
    - `vol_target(tau=0.20 [0.05–0.40], idm="auto"|float ≤ 2.5, max_gross=1.0)`. The weight is `w_i = tau·IDM·F_i/10/σ_i`, where the combined forecast is `F_i = clip(FDM·Σ_j s_j f_ij, −20, 20)`. `FDM = min(2.5, 1/sqrt(sᵀHs))`, with H the forecast correlation floored at 0. FDM falls back to 1 with fewer than 20 overlapping observations.
    - Gross exposure is always capped at `max_gross` (1.0 for a cash account).
  - `orders.py`: `orders_from_targets(target_weights, portfolio, prices, buffer_fraction=0.10 [0–0.5], min_trade_weight=0.005)`, Carver's buffer:
    - no trade while `|current − target| ≤ buffer_fraction·|target|`;
    - otherwise trade to the nearest buffer edge;
    - a full exit (target 0) always executes;
    - client ids are deterministic.
- **Seam:** `PortfolioConstructor` plus its registry. Strategies choose names and convictions. Constructors choose sizes. The risk layer (BL-11) only cuts.
- **Owns:** `src/stonks/portfolio/{__init__,base,signals,constructors,orders}.py` (new).
- **Parallel:** none. Construction is O(tickers).
- **Tests:**
  - `BuyAndHold`'s 1.0 no longer dominates after normalisation;
  - inverse-vol weights ∝ 1/σ;
  - `vol_target` weights checked by hand on a two-asset fixture;
  - gross exposure never exceeds 1.0;
  - a buffer hit produces no order;
  - a full exit always trades;
  - `single_winner` reproduces a golden fixture of today's tick decision;
  - the registry lists every constructor, and an unknown name raises.
- **Size:** H / M.
- **Deps:** BL-09 (same package).

#### BL-09 Volatility toolkit and cross-sectional helpers
- **Sources:** Tsay; Sinclair (estimators); Yang & Zhang; Carver (EWMA span 35 and floor); Velu et al.; Tulchinsky (rank, zscore, neutralise); Gray & Vogel.
- **Problem:** `features/library.py` has no volatility estimator and no cross-sectional helper. `features/indicators.py:27` offers ATR only.
- **Spec:**
  - `features/volatility.py`, pure numpy/pandas, returning a σ series per bar:
    - `close_to_close(n)`;
    - `ewma_vol(returns, span=35)` (`λ = 1−2/(span+1)`), plus `riskmetrics_vol(λ=0.94)`;
    - `parkinson(n)`, `garman_klass(n)`, `rogers_satchell(n)`;
    - `yang_zhang(n)`, using the open-to-close σ_c, not close-to-close (Strimpel's repo gets this wrong), with `k = 0.34/(1.34+(n+1)/(n−1))`;
    - `floor_vol(σ, pct=0.05, window=500)`;
    - `annualize(σ, periods_per_year)`, which reuses `backtest/calendar.py`. Crypto σ_o is about 0.
  - `features/cross_section.py`: `cs_rank`, `cs_zscore(winsor=3)`, `cs_winsorize` and `cs_neutralize(groups)`.
- **Seam:** a feature library of pure functions. A `VolForecaster` ABC comes later (BL-49).
- **Owns:** `features/volatility.py` and `features/cross_section.py` (new).
- **Parallel:** vectorised.
- **Tests:**
  - on a simulated GBM with known σ (seeded), every estimator lands within 10%;
  - YZ equals RS when there are no gaps;
  - EWMA span/λ equivalence;
  - `cs_zscore` has mean 0 and std 1 after winsorising;
  - neutralisation zeroes each group's mean.
- **Size:** M / S.
- **Deps:** none.

#### BL-10 Survival-test registry and suite presets
- **Sources:** principle P44; López de Prado's validation table (stats report §12.1).
- **Problem:**
  - The CLI keeps its own list, `_LAB_TESTS` (`cli.py:947`, checked at `:1062`).
  - The API has a separate `SurvivalTestName` Literal (`app/lab.py:43`) and a factory `_survival_test` (`app/lab.py:353`).
  - The API default suite is `["oos", "period_stability"]` (`app/lab.py:153-155`).
- **Spec:**
  - `lab/survival/registry.py` discovers `SurvivalTest` classes in `lab/survival/*` by their `id`. Each class may declare an `Options` pydantic model and a `build(options, dataset) -> SurvivalTest` classmethod.
  - `survival_test_names()`, `build_survival_test(name, options)`.
  - `SUITE_PRESETS`:
    - `quick = [oos, period_stability]`;
    - `promotion = [oos, walk_forward, deflated_sharpe, pbo, mc_trades, cost_stress, plateau, benchmark_relative, permutation]`.
    - Ids that haven't landed yet are skipped with a logged warning.
  - `app/lab.py` validates names against the registry and accepts `preset`. A request with `register_strategy=True` defaults to `promotion`.
  - The `bind_run(ctx)` hook and its `LabRunContext` come from BL-04 (`lab/trials.py`). The registry only builds tests.
- **Seam:** the registry. A new test is one new file.
- **Owns:** `lab/survival/registry.py` (new), `app/lab.py`. The CLI switch from `_LAB_TESTS` to the registry is an integration hunk.
- **Parallel:** none.
- **Tests:**
  - every existing test is discovered;
  - an unknown name raises and lists the valid ones;
  - a preset with a missing id warns and skips;
  - registering from the API defaults to `promotion`;
  - a new dummy test module in a tmp package is discovered without editing any list.
- **Size:** M / S.
- **Deps:** none.

#### BL-11 Risk-rule seam
- **Sources:** Narang (risk model); Elder; Ghosh & Donadio (limits calibrated to the strategy); P28.
- **Problem:** `apply_risk` (`production/risk.py:75-215`) is one hard-coded sequence of caps whose result depends on order. It sees no volatility, history, sector, equity curve or entry dates. `PriceBook` (`production/prices.py:26`) returns closes only.
- **Spec:**
  - An ABC `RiskRule` with `name`, `order: int` and `apply(orders, ctx) -> tuple[list[Order], list[RiskAdjustment]]`. `@register_rule`, with discovery over `production/rules/*`.
  - Today's caps move unchanged into `production/rules/caps.py`: max open positions, per ticker, per asset class, cash buffer, min notional.
  - `RiskContext(portfolio, prices, asset_classes, policy, history: Mapping[ticker, DataFrame] /* last 260 adjusted OHLCV bars */, sectors, equity_curve: Sequence[tuple[date, float]], entry_dates: Mapping[ticker, date], cost_model)`.
  - `build_risk_context(lake, state, portfolio, prices, as_of)` loads the history in one query.
  - The `apply_risk(...)` signature is kept, with a new optional `context=`. Without it, the rules that need history are skipped and the result is logged.
- **Seam:** `RiskRule` plus its registry. BL-27 and BL-28 add rules as new files.
- **Owns:** `production/risk.py`, `production/prices.py`, `production/rules/__init__.py` and `production/rules/caps.py` (new).
- **Parallel:** none.
- **Tests:**
  - behaviour matches today on the existing `test_risk*` suite;
  - a registry-driven generic property test: for every registered rule and random order sets, total buy notional after ≤ before and sells are never blocked. New rules are covered automatically;
  - `build_risk_context` loads the history with one query.
- **Size:** H / M.
- **Deps:** none.

#### BL-12 Multi-strategy construction in the tick and the backtest (retires winner-take-all)
- **Sources:** Carver; Grinold & Kahn (breadth); Narang; Dalio (via Schwager); Pfaff; Meucci; Lewinson.
- **Problem:**
  - `production/tick.py:237-244` gives the whole book to one strategy. `_position_owner` (`tick.py:558`) guesses exits from the latest fill.
  - The backtester lets every strategy decide against one shared portfolio (`backtest/engine.py:210-218`), so each one thinks it owns the book.
  - The ranker sorts across strategies (`ranker.py:94`).
- **Spec:**
  - `portfolio/pipeline.py` provides `build_orders(signals, construction, risk_policy, risk_context, portfolio, prices, ...) -> PipelineResult(orders, target_book, adjustments)`. It is the one function both the tick and the engine call: normalise, construct, `orders_from_targets`, `apply_risk`. Every registered risk rule, including the breaker (BL-28), therefore runs in backtests too.
  - The ranker returns per-strategy raw maps and no longer sorts across strategies.
  - Tick settings `[production.construction]`:
    - `method` (`single_winner` stays the default for one release, then `vol_target`);
    - `mode = "combined" | "sleeves"`. `combined` uses forecasts only. `sleeves` runs each strategy's `decide` against a virtual sub-portfolio, reusing `production/shadow.py`'s virtual books, then nets the orders per ticker;
    - `strategy_weights`, equal by default.
  - A SQLite migration adds `position_attribution(tick_id, ticker, strategy_id, weight_share)`.
  - `production/hooks.py` holds a post-tick hook registry for BL-29 and BL-47.
  - `BacktestConfig.construction: str | None = None`. `None` keeps today's per-strategy `decide`. Any other value runs `build_orders`, building a `RiskContext` from the engine's own curve and history.
- **Seam:** `portfolio/pipeline.py`, the single place construction happens. `PostTickHook` has its own registry.
- **Owns:** `production/tick.py`, `production/ranker.py`, `production/shadow.py`, `production/hooks.py` (new), `portfolio/pipeline.py` (new), `backtest/engine.py`, `store/migrations_sqlite/008_position_attribution.sql`.
- **Parallel:** ranker scoring fans out per strategy (and per ticker chunk when the universe exceeds 500) through `run_tasks` on a read-only snapshot. `[production] scoring_workers` defaults to auto.
- **Tests:**
  - two active strategies both receive capital;
  - `single_winner` matches the pre-change golden tick;
  - attribution sums to 1 per ticker;
  - **parity**: backtest and tick produce identical target weights for the same fixture and date;
  - exits happen without `_position_owner` in combined mode;
  - a post-tick hook runs once per tick.
- **Size:** H / L.
- **Deps:** BL-08, BL-11, 8.2.

#### BL-13 Realistic costs by default
- **Sources:** Chan; Carver; Bogle; Wilmott; Kissell.
- **Problem:** `[backtest.costs]` is all zeros (`config/default.toml`). `LabDataset.costs = None` means zero costs (`lab/dataset.py:31-33`). The lab tunes on pre-cost Sharpe (`lab/objectives.py:20-21`).
- **Spec:**
  - `config/default.toml` `[backtest.costs]` takes the values of `CostModelSettings.realistic()` (`backtest/costs.py:126`).
  - The CLI and API lab runs fail loudly (a preflight warning, BL-37) when costs resolve to zero, unless `--allow-zero-costs` is passed.
  - The manifest records the costs in force (BL-06).
  - The tick wiring is 8.2's job.
- **Seam:** `CostModelSettings`, the one cost source.
- **Owns:** integration step of wave 1 (`config/default.toml`).
- **Parallel:** none.
- **Tests:** loading the default settings gives non-zero costs; the zero-cost warning fires.
- **Size:** H / S.
- **Deps:** 8.2.

### Validation gates

#### BL-14 Deflated Sharpe test
- **Sources:** Bailey & López de Prado (2014); López de Prado (*MLAM*, effective N); Carver, Chan and Davey on data snooping.
- **Problem:** the verdict ignores how many configurations were tried (`lab/runner.py:62-77`).
- **Spec:**
  - Test id `deflated_sharpe`, with options `min_dsr=0.95 [0.8–0.99]`, `include_prior_runs=True` and `n_eff_method="effective_rank"|"avg_corr"|"raw"`.
  - Takes the run's trial matrix through `bind_run`.
  - N is `n_trials_class` when `include_prior_runs`, otherwise the run's count. N_eff comes from the trial-return correlation.
  - V is the variance of the per-bar trial Sharpes.
  - SR̂, skew, kurtosis and ρ are measured on the selected strategy's **validation** returns.
  - `SR0 = expected_max_sharpe(N_eff, V)`, and `DSR = psr(SR̂, SR0, T, …)`.
  - Metrics: `n_trials`, `n_eff`, `sr0_per_bar`, `sr_per_bar`, `dsr`, `psr0`.
  - With N = 1, DSR equals PSR(0).
- **Seam:** `SurvivalTest` plus `bind_run`.
- **Owns:** `lab/survival/deflated_sharpe.py` (new).
- **Parallel:** none (matrix math).
- **Tests:** more noise trials lower the DSR; N = 1 equals PSR; a strong fixture passes and a noisy one fails; the test fails with a note when the ledger has no trial matrix.
- **Size:** H / S.
- **Deps:** BL-04, BL-05.

#### BL-15 Probability of backtest overfitting (CSCV)
- **Sources:** Bailey, Borwein, López de Prado & Zhu (2015).
- **Problem:** nothing measures whether the in-sample winner is also the out-of-sample winner.
- **Spec:**
  - Test id `pbo`, with options `n_blocks=10` (even, 8–16), `max_combinations=5000` and `max_pbo=0.2`.
  - Uses the train-window trial matrix.
  - Passes when PBO ≤ 0.2.
  - Metrics: `pbo`, `degradation_slope`, `p_loss`, `n_combinations`.
  - Fails with a note when there are fewer than 8 trials or fewer than 2·n_blocks bars per block.
- **Seam:** `SurvivalTest`, with the math in `stats.pbo`.
- **Owns:** `lab/survival/pbo.py` (new).
- **Parallel:** when combinations exceed 5,000 and are sampled, chunks go through `run_tasks`. Otherwise vectorised.
- **Tests:** pure noise gives PBO around 0.5 and fails; a dominant true column gives around 0 and passes; seeded runs are reproducible; too few trials fails with a note.
- **Size:** H / M.
- **Deps:** BL-04, BL-05, BL-07.

#### BL-16 Statistical out-of-sample gate
- **Sources:** Bailey & López de Prado (PSR, MinTRL); Lo (2002); Carver (a backtested Sharpe above 1.5 is a red flag); Davey (trade minimums).
- **Problem:** `lab/survival/oos.py:12,26`: Sharpe ≥ 0.5 and max drawdown ≥ −30%, with no trade count, no uncertainty, and the same bar for a six-month window as for a five-year one.
- **Spec:**
  - `OutOfSampleTest(mode="psr"|"sharpe"="psr", min_psr=0.95, min_sharpe=0.5, max_drawdown_limit=-0.3, min_trades=20)`.
  - PSR is computed on per-bar validation returns with measured skew, kurtosis and ρ.
  - Added metrics: `psr0`, `sharpe_se`, `sharpe_ci_low` and `sharpe_ci_high` (stationary bootstrap), `min_trl_bars`, `n_trades`, `trade_expectancy`.
  - Fails with a note when `n_trades < min_trades`.
  - `mode="sharpe"` keeps the legacy behaviour.
- **Seam:** the existing test.
- **Owns:** `lab/survival/oos.py`.
- **Parallel:** none.
- **Tests:** a short window with Sharpe 0.6 fails in PSR mode; a long window passes; 5 trades fail; legacy mode matches today; the CI brackets the point estimate.
- **Size:** H / S.
- **Deps:** BL-02, BL-03, BL-05.

#### BL-17 Monte Carlo over the trade sequence
- **Sources:** Davey (risk of ruin, return/drawdown, probability of profit; thresholds are his conventions, so they are configurable); Ehlers & Way; Vince.
- **Problem:** there is no trade-level resampling, no risk of ruin, and no forward-looking drawdown bound (`backtest/report.py:43-57`).
- **Spec:**
  - Test id `mc_trades`, options: `n_paths=5000 [1000–20000]`, `ruin_drawdown=0.40`, `min_trades=30`, `max_risk_of_ruin=0.10`, `min_return_to_dd=2.0 [0.5–4]`, `min_prob_profit=0.8`.
  - Takes validation trades, or stitched walk-forward trades when present.
  - Each trade's contribution is `pnl/equity_at_entry`, which makes it independent of size.
  - Resamples one year of trades with replacement (`n_per_year` = the observed trade rate) per path, compounds, and stops a path at ruin.
  - Metrics: `risk_of_ruin`, `median_max_dd`, `p95_max_dd`, `median_return`, `return_to_dd`, `prob_profit`.
  - `p95_max_dd` and `median_max_dd` are stored under exactly those keys, because BL-25 and BL-29 read them.
  - The seed comes from the run's root seed.
- **Seam:** `SurvivalTest`.
- **Owns:** `lab/survival/mc_trades.py` (new).
- **Parallel:** a single vectorised numpy draw of paths × trades, about 5000 × 250. No processes needed.
- **Tests:** all-winning trades give risk of ruin 0; a coin-flip fixture fails; fewer than 30 trades fails with a note; seeded reproducibility; the metric keys are present.
- **Size:** H / S.
- **Deps:** BL-02.

#### BL-18 Cost stress and cost budget
- **Sources:** Chan (test at 2× costs); Carver (speed limit: cost Sharpe ≤ 1/3 of the pre-cost Sharpe, and ≤ 0.13); Wilmott (stress the least-known inputs); Jansen (cost sensitivity).
- **Problem:** no test changes the cost parameters.
- **Spec:**
  - Test id `cost_stress`.
  - Re-runs the validation backtest at cost multipliers {0, 1, 2, 3}, scaling `half_spread_bps`, `fee_bps`, `fee_flat` and `impact_bps`.
  - Passes when all of these hold:
    - `sharpe(2×) > 0`;
    - `sharpe(2×) ≥ 0.5·sharpe(1×)`;
    - the break-even multiple (the largest m with Sharpe > 0, bisection up to 10) is ≥ 2;
    - `cost_sr = sharpe(0×) − sharpe(1×) ≤ min(0.13, sharpe(0×)/3)`.
  - When the dataset has no costs, the 1× level uses `CostModelSettings.realistic()` and the test notes it.
- **Seam:** `SurvivalTest`. It builds scaled `CostModelSettings` copies and never edits the cost model.
- **Owns:** `lab/survival/cost_stress.py` (new).
- **Parallel:** 4 to 10 backtests as `run_tasks`.
- **Tests:** a zero-turnover strategy passes; a high-turnover fixture fails the speed limit; the break-even multiple is monotonic.
- **Size:** H / S.
- **Deps:** BL-07, BL-13.

#### BL-19 Parameter plateau and cross-instrument consistency
- **Sources:** Kaufman (plateau, not peak; share of profitable tests); Ehlers (robustness ratio); Carver (fit on pooled data).
- **Problem:** the tuner keeps the single best trial (`lab/tuning/random.py`), and its history is dropped (`runner.py:62-67`). Nothing checks the score surface, or whether one ticker carries the result.
- **Spec:**
  - Test id `plateau`: one-at-a-time ± `step` (15% of each numeric range, bounds 0.05–0.3), so 2k neighbours.
    - Each neighbour is scored on the train window with the run's objective and on the validation window with Sharpe.
    - Passes when `median(neighbour train)/best ≥ 0.7 [0.5–0.9]` and `median(neighbour OOS Sharpe) ≥ 0.5·best OOS Sharpe`.
    - Also reports the Ehlers ratio (median trial / best, from the ledger) and the share of profitable trials.
  - Test id `cross_instrument`: runs the tuned params on each ticker alone.
    - Passes when at least 60% [0.5–0.8] of tickers have positive validation expectancy and no ticker has more than 50% of total P&L.
    - Universes with fewer than 3 tickers are marked n/a (pass, with a note).
- **Seam:** `SurvivalTest` with `bind_run`, which provides the setup and the ledger.
- **Owns:** `lab/survival/plateau.py` and `lab/survival/cross_instrument.py` (new).
- **Parallel:** the neighbours (2k tasks) and the tickers (N tasks) go through `run_tasks`.
- **Tests:** a smooth synthetic objective passes; a spike fixture fails; a single-ticker-dominated fixture fails; categorical params are skipped.
- **Size:** H / S.
- **Deps:** BL-04, BL-07.

#### BL-20 Walk-forward upgrades
- **Sources:** Davey (walk-forward efficiency, matrix, default evidence); López de Prado (embargo, stitched-OOS inference); Chan (*Machine Trading*: purging).
- **Problem:** there is no embargo (`lab/dataset.py:50-54`, and `walk_forward.py` uses the same boundary). Folds are averaged, not stitched (`walk_forward.py:184-196`). Folds run serially (`:162`). Walk-forward is not in the default suite.
- **Spec:**
  - `LabDataset.embargo_bars: int = 0`. `val_window` starts `embargo_bars` trading bars after `train_end`, converted through `backtest/calendar.py`.
  - The effective embargo is `max(embargo_bars, getattr(strategy, "label_horizon_bars", 0))`.
  - `LabDataset.benchmark: str = "auto"` is added here and consumed by BL-22.
  - `WalkForwardTest`:
    - stitches the fold OOS returns and reports `psr0_stitched` and `sharpe_stitched`;
    - reports `wfe = annualised OOS return / annualised IS return`, and requires `wfe ≥ 0.5 [0.3–1.0]` on top of the positive-share rule;
    - offers an optional `matrix` (train {504, 756, 1008} × test {126, 252}; pass when at least 2/3 of cells pass), off by default;
    - runs folds in parallel.
  - Walk-forward is in the `promotion` preset (BL-10).
- **Seam:** `LabDataset`, plus the existing test.
- **Owns:** `lab/dataset.py`, `lab/survival/walk_forward.py`.
- **Parallel:** folds as `run_tasks`, with each fold's re-tune running in-process. 4 to 12 folds × a matrix of 6 cells gives up to 72 tasks.
- **Tests:** the embargo shifts the validation start; the label horizon wins over a smaller embargo; the stitched PSR equals the PSR of the concatenated series; a WFE below 0.5 fails; serial and parallel give identical fold scores.
- **Size:** H / M.
- **Deps:** BL-05, BL-07.

#### BL-21 Out-of-sample windows for every test, and a promotion-grade MCPT
- **Sources:** López de Prado; the stats report's §12.2 and §12.4.
- **Problem:** `perturbation.py:58,70`, `runs_test.py:34` and `period_stability.py` backtest `full_window`, which includes the tuned data. MCPT defaults to `n_permutations=50` (`permutation.py:216`), where the minimum p is 0.0196.
- **Spec:**
  - These tests take `window: Literal["val","full"]="val"`. Period-stability sub-windows cover the validation window.
  - Perturbation runs are parallel.
  - The MCPT `promotion` options are `n_permutations=200` (minimum p ≈ 0.005) and `retune=True` for ML strategies (a strategy with a non-trivial `fit`). This builds on 6.6's pool and its walk-forward permutation test.
- **Seam:** the existing tests.
- **Owns:** `lab/survival/perturbation.py`, `runs_test.py`, `period_stability.py`, `permutation.py`.
- **Parallel:** perturbation runs and permutations as `run_tasks`, each with its own spawned seed.
- **Tests:** the default window is `val`; `full` reproduces today; parallel equals serial.
- **Size:** M / S.
- **Deps:** BL-07, 6.6.

#### BL-22 Benchmark everywhere, and a benchmark-relative test
- **Sources:** Bogle; Malkiel; Grinold & Kahn; Hazlitt; Kahneman (the outside view); Pik & Ghosh (tear sheets).
- **Problem:** there is no benchmark in `BacktestReport` (`report.py:43-57`), in the OOS gate (`oos.py:26`), in reporting (`reporting/data.py`) or in go-live (`production/golive.py`). `BuyAndHold` exists but never runs as a baseline.
- **Spec:**
  - `backtest/benchmark.py` provides `benchmark_curve(lake, spec, dates)`. `spec` is `"auto"` (use `SPY.US` if the lake has it, else `"EW"`), `"EW"` (equal-weight buy-and-hold of the universe from the first date) or any ticker. Adjusted prices are used.
  - `BenchmarkStats`: `benchmark_cagr`, `excess_cagr`, `beta`, `alpha_annual`, `alpha_tstat` (HAC), `tracking_error`, `information_ratio = mean(r−r_b)/σ(r−r_b)·√ppy`, `up_capture`, `down_capture`, `benchmark_max_dd`, `correlation`.
  - `lab/backtesting.run_backtest` attaches it, reading `getattr(dataset, "benchmark", "auto")`.
  - Test id `benchmark_relative`:
    - passes when `information_ratio ≥ min_ir (0.0)` and `excess_cagr ≥ 0.0` on the validation window;
    - `require_alpha_tstat: float | None = None` is available (2.0 is Chan's alpha test).
  - Reporting adds the benchmark line, a top-5 drawdown table (depth, start, trough, recovery, length), a monthly returns grid and a rolling 126-bar Sharpe.
- **Seam:** `backtest/benchmark.py` is the one benchmark source. Reporting and survival tests read `report.benchmark`.
- **Owns:** `backtest/benchmark.py` (new), `lab/backtesting.py`, `lab/survival/benchmark_relative.py` (new), `reporting/*`.
- **Parallel:** none.
- **Tests:** a strategy identical to the benchmark gives IR 0 and beta 1; a leveraged copy has beta about 2; `"auto"` falls back to EW; the drawdown table on a fixture; the test passes and fails as expected.
- **Size:** H / M.
- **Deps:** BL-01, BL-05.

#### BL-23 Market-beta attribution
- **Sources:** Chan (*Machine Trading*, alpha test); Lewinson (factor attribution); Grinold & Kahn.
- **Problem:** nothing separates alpha from beta. A long-only strategy tested in a bull window passes on beta alone.
- **Spec:**
  - `backtest/benchmark.py` also provides `attribute(returns, benchmark_returns) -> (alpha_annual, beta, alpha_tstat_hac, r2, residual_sharpe)`.
  - `benchmark_relative` reports `beta` and `alpha_tstat` whatever it decides.
  - Fama-French factors are parked until a vendor-agnostic factor series exists in `macro_indicators`.
- **Seam:** part of BL-22.
- **Owns:** `backtest/benchmark.py` (same package as BL-22).
- **Parallel:** none.
- **Tests:** synthetic returns `r = 0.0002 + 1.5·r_m + ε` recover beta ≈ 1.5 and a positive alpha t-stat.
- **Size:** M / S.
- **Deps:** BL-05.

### Governance, incubation and pre-commitment

#### BL-24 Promotion governance and an intervention log
- **Sources:** Kahneman (*Thinking, Fast and Slow*; *Noise*); Tendler; Steenbarger; Part 7 of the Axon series.
- **Problem:** `registry/store.py:104-112` changes a status with no reason and no audit row. `app/strategies.py:108-115` promotes unconditionally. The CLI `registry promote` (`cli.py:705-706`) ignores go-live.
- **Spec:**
  - SQLite `007_status_changes.sql`: `status_changes(id, strategy_id, kind /* status|risk_reset|manual_order|config */, from_status, to_status, actor, reason, golive_passed, golive_report_json, created_at)`.
  - `StrategyRegistry.set_status(id, status, *, actor, reason, golive_report=None, override=False)` writes the row in the same transaction.
  - Moving to `active` requires a passing go-live report, or `override=True` with a reason of at least 20 characters.
  - `retire` requires a reason.
  - The app service methods take `reason` and `override`. The CLI, API and MCP flags are integration hunks; MCP already requires `confirm`.
  - `registry show` lists the history. Promotion warns when `hypothesis` is empty (BL-26).
- **Seam:** `StrategyRegistry` is the only writer of status, and every entry point goes through the service.
- **Owns:** `store/migrations_sqlite/007_status_changes.sql`, `registry/store.py`, `app/strategies.py`.
- **Parallel:** none.
- **Tests:** promoting without a go-live pass raises; an override with a short reason raises; every change writes one row; retire without a reason raises; the API path uses the same rules.
- **Size:** H / S.
- **Deps:** none.

#### BL-25 Incubation-grade go-live
- **Sources:** Davey (6-12 months of incubation, inside the Monte Carlo band); López de Prado (MinTRL).
- **Problem:** `GoLivePolicy` needs 20 days and 5 trades (`config.py:173,180`). Drift compares compounded CAGR (`production/golive.py:209-218`).
- **Spec:**
  - Defaults move to `min_days=63` and `min_trades=20`.
  - `use_min_trl=True` makes the day requirement `max(min_days, MinTRL(backtest OOS SR, skew, kurt) in days, capped at 252)`.
  - New check `within_mc_band`: live max drawdown ≤ stored `p95_max_dd`, and the live annualised return ≥ the Monte Carlo 5th-percentile return scaled to the period.
  - Missing Monte Carlo data fails the check, which fits the "missing data never passes" rule.
  - The report includes `n_trials_class`, DSR, PBO, the benchmark excess and the premortem text: the promotion checklist.
- **Seam:** go-live checks, one function per check.
- **Owns:** `production/golive.py`.
- **Parallel:** none.
- **Tests:** MinTRL raises the day requirement for a low Sharpe; the band check passes and fails; the checklist fields are present.
- **Size:** H / S.
- **Deps:** BL-05, BL-17.

#### BL-26 Strategy metadata (hypothesis card and capability hooks)
- **Sources:** López de Prado (*Causal Factor Investing*); Narang (alpha families); Chan (premise); Harris (name the counterparty); Grimes (per-class edges).
- **Problem:** strategies record no mechanism, family, label horizon or data need (`strategies/base.py:18-40`). The asset classes are a fixed ClassVar (`base.py:26`).
- **Spec:** `BaseStrategy` class variables:
  - `hypothesis: str = ""`: mechanism, expected sign, counterparty, and when the strategy should fail;
  - `alpha_family: Literal["trend","reversion","carry","value","quality","growth","sentiment","data_driven","benchmark","other"] = "other"`;
  - `premise: Literal["trend","mean_reversion","none"] = "none"`;
  - `label_horizon_bars: int = 0`;
  - `required_history_bars: int = 0`.

  An optional instance param `asset_classes` overrides `applicable_asset_classes`, for opt-in use of a strategy outside its evidence base. `meta.json` and `registry show` carry the metadata.
- **Seam:** duck-typed class attributes read with `getattr`.
- **Owns:** `strategies/base.py`.
- **Parallel:** none.
- **Tests:** defaults exist on every catalogued strategy; the `asset_classes` override is honoured by the ranker filter; an empty override is rejected.
- **Size:** M / S.
- **Deps:** none.

#### BL-27 Risk rules: position risk, portfolio volatility, drawdown scaling, liquidity, sector, holding time
- **Sources:** Elder (2% rule); Vince; Carver (risk overlay, correlation shock); Schwager/Benedict and Ghosh & Donadio (drawdown scaling, holding time); Karasan and Harris (liquidity); Grinold & Kahn (sector).
- **Problem:** `RiskPolicy` (`config.py:112-134`) has only permissive caps (`max_weight_per_ticker=1.0`). There are no volatility, drawdown, liquidity or sector controls.
- **Spec:** one file per rule under `production/rules/`, each with its own settings model:
  - `risk_per_position`: `max_risk=0.02 [0.0025–0.05]` of equity. Position risk is `qty·k·ATR(20)` with `k=3`, or `qty·price·σ_daily·2.33` (one-day 99% VaR). Buys are clipped.
  - `portfolio_vol`: ex-ante `sqrt(wᵀΣw)` from an EWMA covariance (span 60) of the context history. When it exceeds `vol_cap` (default 0.25 annual), buys scale by `vol_cap/est`. The correlation-shock bound `Σ|w_i|σ_i ≤ shock_cap` defaults to 0.40.
  - `drawdown_scaling`: `schedule=[(0.05,1.0),(0.10,0.5),(0.15,0.25)]` on drawdown from the peak of `equity_curve`. Buy quantities are multiplied by the scale. The scale returns to 1.0 only once drawdown is below the first threshold (hysteresis).
  - `liquidity`: `max_pct_adv=0.01` of the 20-bar median dollar volume. `min_median_dollar_volume=1e6` drops a name. An optional Amihud ceiling is available.
  - `sector_cap`: `max_weight_per_sector: float | None = None`, using `instruments.sector`.
  - `max_holding`: `max_holding_bars: int | None = None`. Forces a sell of positions older than N bars since the entry date. This protects against zombie positions left by retired strategies.
- **Seam:** `RiskRule` plus its registry (BL-11). BL-12's pipeline runs the same rules in backtests.
- **Owns:** `production/rules/{risk_per_position,portfolio_vol,drawdown_scaling,liquidity,sector_cap,max_holding}.py` (new).
- **Parallel:** none.
- **Tests:** each rule gets a hand-worked example. The generic never-increases-exposure property test from BL-11 covers them all. Also: hysteresis on drawdown scaling; `max_holding` emits a sell.
- **Size:** H / M.
- **Deps:** BL-09, BL-11.

#### BL-28 Circuit breaker and operational gating
- **Sources:** Elder (6% rule); Benedict (via Schwager); Ghosh & Donadio; Part 7 (graded kill switches; crypto trades while you sleep).
- **Problem:** nothing halts trading after a bad week or month. `production/health.py` alerts on stale data and stuck runs but doesn't gate the tick.
- **Spec:**
  - `production/rules/circuit_breaker.py` settings:
    - `max_month_loss=0.06`, measured from the first snapshot of the calendar month, plus open risk to stops when stops are known;
    - `max_week_loss=0.04` over a rolling 5 sessions;
    - `max_drawdown_halt=0.20` from the all-time peak, which **latches**;
    - `cooldown="rest_of_month"`.
    - When tripped, it drops every buy and lets sells and exits through.
  - `operational_halt` rule: stale data or a stuck run found by `health` blocks buys.
  - Migration `009_risk_halts.sql`: `risk_halts(id, kind, tripped_at, reason, cleared_at, cleared_by, clear_reason)`.
  - `production/halts.py` provides `clear_halt(kind, actor, reason)` and writes a `status_changes` row with `kind='risk_reset'`.
  - Trips send `critical` notifications through `notify/`.
  - Because BL-12's pipeline is shared, the breaker also runs in backtests, so its effect on CAGR and drawdown shows in the lab.
- **Seam:** `RiskRule`, plus a halt store.
- **Owns:** `production/rules/circuit_breaker.py`, `production/rules/operational_halt.py`, `production/halts.py`, `production/health.py`, `store/migrations_sqlite/009_risk_halts.sql`.
- **Parallel:** none.
- **Tests:** a 6% month loss blocks buys and lets sells through; the latched halt survives a new month until cleared; clearing writes an audit row; stale data gates buys; a backtest with the breaker has a smaller drawdown on a crash fixture.
- **Size:** H / M.
- **Deps:** BL-11, BL-12, BL-24.

#### BL-29 Quit rule ("the system is broken")
- **Sources:** Davey (stop trading beyond the Monte Carlo worst case or 1.5× the historical drawdown); Kahneman (pre-commitment).
- **Problem:** go-live checks the paper period once. Nothing watches an active strategy afterwards (`production/golive.py`).
- **Spec:**
  - A post-tick hook (BL-12 registry): for each active strategy, compute the attributed drawdown since promotion from `position_attribution` P&L.
  - When it exceeds `quit_multiple=1.5 [1.0–3.0]` × the backtest OOS maximum drawdown, or the stored `mc_trades.p95_max_dd`, send an `error` notification.
  - When `auto_demote=true` (default false), set the status to `shadow` with `actor="system"` and a reason (BL-24 audit).
  - Minimum evaluation horizon: `retire` for underperformance before this fires, or before `min_eval_days=126`, needs `override`.
- **Seam:** `PostTickHook`.
- **Owns:** `production/quit_rule.py` (new).
- **Parallel:** none.
- **Tests:** a drawdown beyond the p95 alerts; auto-demote writes an audit row; a missing Monte Carlo report falls back to the multiple.
- **Size:** H / S.
- **Deps:** BL-12, BL-17, BL-24.

### Execution realism and TCA

#### BL-30 Participation cap, partial fills and order types in the simulator
- **Sources:** Kissell; Harris; Johnson (bar-based limit and stop fills); Zipline's `VolumeShareSlippage` (via Strimpel); LEAN reality models.
- **Problem:**
  - `SimulatedBroker.place_order` (`backtest/simulated_broker.py:77-131`) fills the full quantity whatever the volume, and ignores `order_type` and `limit_price`.
  - A zero-volume bar still fills, at the maximum impact (`costs.py:162-163`).
  - Fills happen after any gap (`engine.py:186`).
- **Spec:**
  - `backtest/fills.py` defines `FillModelSettings(max_participation=0.10, carry_unfilled=True, allow_zero_volume=False, honour_limits=True, max_gap_days=7)`.
  - Quantity is clipped to `ρ·bar_volume`, or to `ρ·ADV_lagged` when bar volume is missing. The remainder is re-queued by the engine when `carry_unfilled`, and a new rebalance still replaces queued orders.
  - Limit and stop rules:
    - A buy limit fills if `low ≤ L`, at `min(open, L)`.
    - A sell limit fills if `high ≥ L`, at `max(open, L)`.
    - A buy stop triggers if `high ≥ S` and fills at `max(open, S)`.
    - A sell stop triggers if `low ≤ S` and fills at `min(open, S)`.
    - DAY orders expire.
  - No fill after a gap longer than `max_gap_days`.
  - The engine passes highs, lows and the lagged ADV to the broker.
- **Seam:** a `FillModel` inside `SimulatedBroker`.
- **Owns:** `backtest/fills.py` (new), `backtest/simulated_broker.py`, `backtest/engine.py`.
- **Parallel:** none.
- **Tests:** a 10% cap splits an order across bars; zero volume gives no fill; an untouched limit doesn't fill; a gapped stop fills at the open; a gap guard; the property test cash ≥ 0 still holds.
- **Size:** H / M.
- **Deps:** BL-01.

#### BL-31 Volatility-aware impact and per-ticker spreads
- **Sources:** Kissell (I-Star); Bacidore (`half-spread + γσ√(Q/V)`); Almgren et al.; Karasan (Corwin-Schultz, Roll); Abdi & Ranaldo.
- **Problem:** impact has no volatility term and uses the fill bar's own volume (`costs.py:157-164`, `engine.py:186-188`). The half-spread is a constant per asset class (`costs.py:132-138`).
- **Spec:**
  - `CostModelSettings.impact_model = "sqrt" | "sqrt_vol" | "istar"`, with default `sqrt_vol`: `impact_bps = γ·σ_daily_bps·sqrt(Q/ADV)`, `γ=1.0`, capped by `max_impact_bps`.
  - `istar` settings: `a1=708, a2=0.55, a3=0.71, a4=0.5, b1=0.98, pov=0.1` (verify).
  - `Trade` gains `adv` (median volume of the prior 20 bars) and `sigma_daily` (prior 20 bars). Both are computed point in time with `shift(1)` and precomputed once in `_load_bars`.
  - `half_spread_model = "class" | "corwin_schultz" | "abdi_ranaldo"`: a per-ticker estimate from the prior 21 bars, clipped to [class floor, 200 bps].
  - The monotonicity contract (`costs.py:26-28`) is kept.
- **Seam:** `CostModel`.
- **Owns:** `backtest/costs.py`, plus `features/spread.py` (new).
- **Parallel:** vectorised rolling windows.
- **Tests:** at equal Q/ADV, higher σ costs more; I-Star reduces to the square-root law when a2=0.5 and a3=0; Corwin-Schultz on a known fixture; no look-ahead (changing the fill bar doesn't change `adv`); property test of monotonicity in quantity.
- **Size:** M / M.
- **Deps:** BL-30 (same package).

#### BL-32 TCA and the decision journal
- **Sources:** Kissell and Perold (implementation shortfall); Bacidore (arrival price); Harris; Lewis (*Flash Boys*); Johnson (multi-benchmark slippage); Elder (journal).
- **Problem:** `core/types.py` `Order` has no decision price, and `Fill` has only `price` and `fee`. The `orders` and `fills` tables carry no benchmark price. Paper fills can't be compared with the backtest's assumptions.
- **Spec:**
  - `Order` gains `decision_price: float | None` and `decision_context: Mapping | None`. The context holds the feature snapshot, the estimate, the rank, the constructor, and `trigger ∈ {signal, exit_no_pick, stop, breaker, holding_time, manual}`.
  - Migration `010_tca.sql`: `orders.decision_price`, `orders.decision_at`, `orders.decision_context_json`, `fills.arrival_price`, `fills.benchmark_price`.
  - `production/tca.py` computes, in bps:
    - `is_bps = s·(fill/decision − 1)·1e4`;
    - delay `s·(arrival/decision − 1)`;
    - trading `s·(fill/arrival − 1)`;
    - opportunity for unfilled orders `s·(next_close/decision − 1)`;
    - the model-predicted bps from the same `CostModel`, for comparison.
  - `refresh_arrival_prices(state, lake)` fills `arrival_price` (the next session open) once bars are ingested.
  - Service functions `tca_report(since)` and `journal(since)` (closed round trips with entry and exit reasons, MAE and MFE). The CLI and API are integration hunks.
  - Any remaining gap between the simulated tick's fill convention and the backtest's after 8.2 is recorded as the `convention` component.
- **Seam:** TCA sits in `production/tca.py`. The tick only records `decision_price` and `decision_context`.
- **Owns:** `core/types.py`, `store/migrations_sqlite/010_tca.sql`, `production/tca.py` (new), `production/tick.py`, `execution/reconcile.py`.
- **Parallel:** none.
- **Tests:** IS by hand for a buy and a sell; opportunity cost for a rejected order; decision context persisted; journal round trips match the ledger (BL-02).
- **Size:** M / M.
- **Deps:** BL-02, BL-12.

### Signal research

#### BL-33 Signal IC analysis
- **Sources:** Grinold & Kahn (IC, the fundamental law); Tulchinsky (decay, turnover); Jansen (HAC inference); Strimpel (AlphaLens workflow).
- **Problem:** the lab goes straight from strategy to tuning. Nobody measures whether `estimate_return` ranks future returns.
- **Spec:**
  - `lab/signal_eval.py` provides `signal_ic(strategy, dataset, horizons=(1,5,21,63), every_bars=5)`.
    - Raw scores over universe × dates.
    - Forward adjusted returns from the next open.
    - The Spearman IC per date; mean IC; IC std; ICIR; HAC t-stat (lag h−1); IC decay by horizon; quintile spread returns; score turnover (1 − the rank autocorrelation).
    - Returns a `SurvivalReport` with id `signal_ic` that is informational (always passes).
    - Universes with fewer than 10 tickers are n/a.
  - The mean IC at the strategy's horizon is stored in meta as `ic_estimate`, and BL-08's `alpha` normalisation uses it.
- **Seam:** a lab tool plus an informational `SurvivalTest`.
- **Owns:** `lab/signal_eval.py` (new).
- **Parallel:** date chunks as `run_tasks`.
- **Tests:** a planted signal (score equals the next return) gives IC about 1; noise gives about 0 with |t| < 2; seeded; a HAC SE above the iid SE on overlapping horizons.
- **Size:** M / M.
- **Deps:** BL-05, BL-07.

#### BL-34 Event study against baseline drift
- **Sources:** Grimes (conditional returns minus baseline, per asset class; breakouts are negative in single stocks).
- **Problem:** nothing checks that an entry signal beats the unconditional drift, or that its sign matches the asset class it trades (`donchian_breakout.py:58`, which defaults to `AAPL.US`).
- **Spec:**
  - Test id `event_study`, options `horizons=(1,5,10,20,60)`, `min_events=100 [30–500]`, `alpha=0.05`.
  - Events are the bars where a ticker newly enters the strategy's picks.
  - Excess return is the event mean minus the mean over all bars of the same tickers and period.
  - Significance comes from a block bootstrap of events (1000 draws, seeded).
  - Passes when excess is above 0 at the typical holding horizon (the ledger's average bars held, else 20) with p ≤ α.
  - Reports per asset class.
  - Fewer than `min_events` fails with a note.
- **Seam:** `SurvivalTest`.
- **Owns:** `lab/survival/event_study.py` (new).
- **Parallel:** event extraction per ticker as `run_tasks`.
- **Tests:** a planted edge passes; a random entry fails; the per-class split; too few events.
- **Size:** H / S.
- **Deps:** BL-02, BL-05, BL-07.

#### BL-35 Versus-random test
- **Sources:** Woodriff (via Schwager): run the same search on noise and beat its best.
- **Problem:** MCPT permutes prices (`lab/survival/permutation.py`), but doesn't compare the tuner's best with the best the same budget finds in noise.
- **Spec:**
  - Test id `vs_random`, options `k=20`, `block=20`, `quantile=0.95`.
  - Builds K noise lakes by block-bootstrapping real log returns (OHLC rebuilt as in `permutation.py`) over the train window.
  - Runs the full bound tuner with the same budget on each and records the best score.
  - Passes when the real best exceeds the noise bests' 95th percentile **and** the real OOS score exceeds the median noise OOS score.
- **Seam:** `SurvivalTest` plus `bind_run`.
- **Owns:** `lab/survival/vs_random.py` (new).
- **Parallel:** K tasks, each with an in-process inner tuner. 20 tasks fill 20 of the 32 cores.
- **Tests:** a strategy with a planted edge passes; a pure-noise strategy fails; results are identical across worker counts.
- **Size:** H / M.
- **Deps:** BL-04, BL-07.

### Data integrity

#### BL-36 Statement audit
- **Sources:** Tracy; Ittelson (the three statements are one system); Graham & Dodd.
- **Problem:** the `upsert_<statement>` functions (`store/lake.py`) never check accounting identities. Broken vendor rows feed `QualityValue`.
- **Spec:**
  - `store/audit.py` runs set-based DuckDB checks:
    - `|TA − (TL + equity + NCI)|/TA > 2%`;
    - `|NI_income − NI_cashflow|/|NI| > 5%`;
    - `|end_period_cash − cash_and_equivalents|/cash > 5%`;
    - `|GP − (revenue − COGS)|/revenue > 2%`;
    - the sum of 4 quarters vs the annual figure > 5%;
    - `filing_date < period_end`.
  - Flags go to `statement_flags(ticker, period_end, frequency, check, severity, detail, flagged_at)` (DuckDB migration `011`).
  - Lake helper `get_statement_flags(ticker)`.
- **Seam:** an audit module. Strategies opt in to skipping flagged rows.
- **Owns:** `store/audit.py` (new), `store/migrations_duckdb/011_statement_flags.sql`, plus additive helpers in `store/lake.py`.
- **Parallel:** set-based SQL, no processes.
- **Tests:** each check on a planted bad row; clean data produces no flags; the audit is idempotent.
- **Size:** M / M.
- **Deps:** none.

#### BL-37 Point-in-time universes and a lab preflight
- **Sources:** Malkiel; Clenow; Covel/Wilcox & Crittenden; Chan (survivorship); the D-2 long-history requirement.
- **Problem:**
  - Universes are static ticker lists (`lab/dataset.py:26`, `TickSettings.universe`).
  - `instruments.is_delisted` (`001_init.sql:8`) is never used for selection.
  - Nothing warns when a strategy needs more history than the lake holds (the free tier gives at most one year).
- **Spec:**
  - DuckDB `012_universe_membership.sql`: `universe_membership(universe_id, ticker, start_date, end_date)`.
  - Lake helpers `upsert_universe_membership` and `members_as_of(universe_id, date)`.
  - `lab/universe.py` provides `resolve(universe_ref, as_of)` for an id, a static list, or a rule `{min_adv, asset_classes, exclude_sectors}` that includes delisted names before their delisting.
  - `lab/preflight.py` returns warnings (errors with `--strict`) for:
    - `required_history_bars` beyond coverage;
    - no delisted names in a window of 5 years or more;
    - zero costs;
    - a missing benchmark.
  - `LabRunner.run` calls the preflight and stores the warnings in `lab_runs`.
  - The engine and ranker wiring comes in BL-49.
- **Seam:** a universe resolver plus the preflight.
- **Owns:** `store/migrations_duckdb/012_universe_membership.sql`, `store/lake.py` (additive), `lab/universe.py` (new), `lab/preflight.py` (new), `lab/runner.py` (the preflight call).
- **Parallel:** none.
- **Tests:** membership over a date range; a delisted name included before its date; each preflight warning; strict mode raises.
- **Size:** H / M.
- **Deps:** BL-04, BL-26.

### Strategies

#### BL-38 `QuantMomentum` (Gray & Vogel)
- **Sources:** Gray & Vogel (*Quantitative Momentum*); Da, Gurun & Warachka (frog in the pan); Barroso & Santa-Clara (vol scaling).
- **Problem:** `Momentum` uses a 20-day lookback (`momentum.py:36-41`), which is the reversal horizon, with no skip month. It holds a single name with all its cash (`momentum.py:104-105`) and has no path-quality filter.
- **Spec:**
  - Params: `formation_bars=252`, `skip_bars=21`, `top_pct=0.10`, `id_keep_pct=0.5`, `rebalance_months=(2,5,8,11)` (last trading day), `vol_scale_target: float | None = None` (Barroso: `σ_target/σ̂_126`, suggested 0.12), `absolute_momentum: bool = False` (12-2 return > 0).
  - Score: `r = C_{t−21}/C_{t−252} − 1` on adjusted closes.
  - `ID = sign(r)·(%neg − %pos)` over the daily returns of the same window.
  - `score_universe` keeps the top decile by r, then the lowest-ID half, and gives each kept name conviction 1.0. Equal weight is left to the constructor.
  - The market filter is **not** built in: compose it with `RegimeFilter` (BL-42).
  - Metadata: `alpha_family="trend"`, `required_history_bars=273`, `label_horizon_bars=63`, and a hypothesis (investor underreaction to gradual information).
- **Seam:** `Strategy`, with `score_universe` and `features/cross_section`.
- **Owns:** `strategies/examples/quant_momentum.py` and `features/momentum.py` (new: `trailing_return_skip`, `information_discreteness`, `is_quarter_rebalance_day`).
- **Parallel:** through the lab (trials, walk-forward, MCPT).
- **Tests:** a hand-computed 12-2 return; ID of −1 for all-positive days; selection on a 20-ticker fixture; rebalance only in months 2, 5, 8 and 11; no look-ahead at the skip boundary.
- **Size:** H / M.
- **Deps:** BL-01, BL-08, BL-09.

#### BL-39 Stocks on the Move (Clenow), with an ATR risk-parity constructor
- **Sources:** Clenow (*Stocks on the Move*; *Trading Evolved*).
- **Problem:** there is no regression-momentum score, gap filter or ATR risk-parity sizing, and a raw 20-day return is used for ranking (`momentum.py:134-137`).
- **Spec:**
  - Strategy params: `lookback=90 [60–250]`, `ma_filter=100`, `gap_limit=0.15`, `top_fraction=0.2`, `annualization=250`.
  - Score: `(exp(b)^250 − 1)·R²`, where b is the OLS slope of `ln(adj_close)` over `lookback`.
  - A name is eligible when close > SMA_100, `max|daily return| < gap_limit`, and score > 0.
  - Sells when a name leaves the top 20%, drops below SMA_100, or gaps.
  - `rebalance_every_bars=5`.
  - The index filter comes by composition with `RegimeFilter(price_trend(SPY.US, 200), mode="block_new_buys")`.
  - New constructor `atr_parity(risk_factor=0.001, atr_period=20, resize_every_bars=10, resize_tolerance=0.10)`: `shares = equity·risk_factor/ATR20`. It registers itself through BL-08's decorator.
- **Seam:** `Strategy` plus `PortfolioConstructor`. Sizing never lives inside the strategy.
- **Owns:** `strategies/examples/stocks_on_the_move.py`, `features/trend.py` (`regression_momentum`, `max_gap`), `portfolio/atr_parity.py` (all new).
- **Parallel:** through the lab.
- **Tests:** the slope·R² score on a clean exponential series; the gap filter; `atr_parity` shares by hand; resize tolerance.
- **Size:** H / M.
- **Deps:** BL-01, BL-08, BL-42 (for the composition test).

#### BL-40 Cross-asset trend following
- **Sources:** Carver (EWMAC); Clenow (*Trading Evolved*, time-series momentum); Hurst, Ooi & Pedersen; Covel and Wilcox & Crittenden (all-time-high trend, ATR trailing stop); Kaufman.
- **Problem:** there is no diversified, cross-asset trend strategy. Donchian trades one ticker (`donchian_breakout.py:58,111-118`). There is no trailing stop anywhere.
- **Spec:**
  - `EWMACTrend(speeds=(8,16,32,64))`:
    - `raw = (EMA_f − EMA_4f)/σ_price`, where σ_price is the EWMA (span 36) std of daily price changes;
    - fixed scalars `{8:5.3, 16:3.75, 32:2.65, 64:1.87}` (verify), re-estimated as `10/mean|raw|` pooled in the lab;
    - the forecast is the FDM-weighted mean, capped at ±20; long-only clips at 0;
    - all four asset classes; `alpha_family="trend"`.
  - `TimeSeriesMomentum(lookbacks=(125,250), mode="sign"|"scaled")`: the mean of `sign(r_L)`, or `r_L/σ_L` capped; `rebalance_every_bars=21`.
  - `AllTimeHighTrend(atr_bars=50 [40–60], k_atr=10 [3–12])`: entry when the weekly close is at an all-time high; for equity and crypto.
  - `TrailingStopWrapper(inner, k_atr=3, atr_period=20, cooldown_bars=5)`:
    - `stop = HWM_since_entry − k·ATR`, never lowered;
    - exits when close < stop;
    - stateless, because the high-water mark is rebuilt from bars since the entry;
    - follows the `MacroRegimeFilter` and `LastTradeFilter` wrapper pattern (`strategies/_wrapping.py`).
  - Sizing goes through the `vol_target` constructor.
- **Seam:** `Strategy` plus a wrapper. Forecasts feed BL-08.
- **Owns:** `strategies/examples/{ewmac_trend,time_series_momentum,ath_trend}.py`, `strategies/trailing_stop.py`, `features/trend_following.py` (all new).
- **Parallel:** through the lab. Cross-instrument consistency (BL-19) is parallel per ticker.
- **Tests:** EWMAC sign on up and down ramps; mean |forecast| of about 10 after scaling; TSMOM sign; an all-time-high entry only at new highs; a stop that never drops; a wrapper exit.
- **Size:** H / M.
- **Deps:** BL-01, BL-08, BL-09.

#### BL-41 `QuantValue` with forensic screens (Gray & Carlisle, Piotroski, Beneish, Greenblatt)
- **Sources:** Gray & Carlisle (*Quantitative Value*); Piotroski (2000); Beneish (1999); Sloan (accruals); Altman; Greenblatt (magic formula, via Schwager); Graham (normalised earnings); Tracy and Ittelson (accruals).
- **Problem:** `QualityValue` uses equity-based value (NI/MC and FCF/MC, `quality_value.py:321-322`), which rewards leverage. It has one period of quality (`:323-326`), fixed min-max clips instead of cross-sectional percentiles (`:58-64`), and a weighted sum rather than "screen, then value, then quality". It has no forensic screen and no sector exclusion.
- **Spec:**
  - `QuantValue(mode="quant_value"|"piotroski"|"magic_formula")`.
  - Universe: exclude sectors `Financial Services`, `Utilities` and `Real Estate`.
  - Forensic step: drop the worst `forensic_drop_pct=0.05` on each of:
    - STA = `(ΔCA − ΔCash − (ΔCL − ΔSTD) − Dep)/TA` (no tax-payable column, so that term is 0; documented);
    - SNOA = `(OA − OL)/TA`;
    - PMAN = `Φ(M)`, where M is the Beneish score `−4.84 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI + 4.679·TATA − 0.327·LVGI`;
    - Altman Z < 1.81.
  - Value: `EBIT/TEV`, where `TEV = mcap + total debt + preferred + NCI − cash_and_short_term_investments`. Keep the top `value_pct=0.10`.
  - Quality = `0.5·pct(FP) + 0.5·pct(FS)`:
    - FP is the percentile average of 8-year geometric-mean ROA, 8-year geometric-mean ROC with `ROC = EBIT/(net PPE + NWC)`, 8-year CFOA, and max(margin growth, margin stability);
    - FS is a 10-point binary score.
  - Hold the top `n=30`, with conviction 1.0. `rebalance_month` is annual, staggered monthly when several sleeves exist.
  - FP degrades to the years available, with `min_years=3`; `years_used` is recorded in the features.
  - `piotroski` mode: top book-to-market quintile, buy when F ≥ 7.
  - `magic_formula` mode: the rank sum of EBIT/EV and ROC, top K, held 252 bars.
  - All reads are point in time by `filing_date`, reusing `_History` and `_trailing_flows` from `quality_value.py`.
  - Metadata: `alpha_family="value"` and a hypothesis (systematic value beats behavioural mispricing; forensic screens avoid permanent loss).
- **Seam:** `Strategy`, with `score_universe` and pure fundamental features.
- **Owns:** `strategies/examples/quant_value.py` and `features/fundamentals.py` (new: `tev`, `ebit_tev`, `roc`, `piotroski_f`, `fs_score`, `beneish_m`, `pman`, `sta`, `snoa`, `altman_z`, `franchise_power`), plus the additive `store/lake.py` helper `get_statement_panel(ticker, as_of, n_annual)`.
- **Parallel:** through the lab. Per-ticker panel loads are DuckDB-bound and batched in one query per rebalance.
- **Tests:** each forensic metric on hand-built statements; the Beneish score on a textbook example; excluded sectors are never picked; a statement filed after `as_of` is invisible; `min_years` degradation.
- **Size:** H / L.
- **Deps:** BL-01, BL-08, BL-09, and paid fundamentals for real use (the free tier returns text errors).

#### BL-42 Composite k-of-n `RegimeFilter`
- **Sources:** Kindleberger & Aliber; Lefevre; Clenow (index 200-day MA); Faber; Elder (Triple Screen); Davey (bull/bear filter).
- **Problem:** `MacroRegimeFilter` reads one series against one threshold (`strategies/macro_regime.py`). There is no price-trend, realised-volatility or yield-curve condition, and no k-of-n logic.
- **Spec:**
  - `features/regime_conditions.py` holds a condition registry:
    - `macro(series, level|change, threshold, lag)`, reusing `macro_regime`'s point-in-time loaders by import;
    - `price_trend(ticker, sma=200, hysteresis=0.0 [0–0.03])`;
    - `realized_vol(ticker, window=21, pct=0.8, lookback_years=5)`;
    - `yield_curve(long="10y", short="3m")` from `macro_indicators`;
    - `higher_timeframe(ticker, interval="1w", ema=26)`.
  - `RegimeFilter(inner, conditions=[...], k=1, mode="block_new_buys"|"exit_all"|"scale")`:
    - risk-off when at least k conditions trigger;
    - `scale` multiplies buy quantities by the share of conditions not triggered;
    - sells always pass.
  - Added to the catalog's `_WRAPPERS` in the integration step.
- **Seam:** wrapper plus condition registry. BL-46 adds VIX conditions as new files.
- **Owns:** `strategies/regime.py`, `features/regime_conditions.py` (new).
- **Parallel:** none.
- **Tests:** each condition on a fixture; k-of-n; block mode lets sells through; exit_all flattens; no look-ahead on macro lags; hysteresis.
- **Size:** H / M.
- **Deps:** BL-01.

#### BL-43 Fix legacy defaults and backfill metadata
- **Sources:** Chan (momentum at 3-12 months); Gray & Vogel (skip month); Grimes (breakouts); Carver (*Leveraged Trading*, vol-scaled stops); Kaufman (ER, KAMA).
- **Problem:** `Momentum.lookback_days` defaults to 20 (`momentum.py:36-41`), the reversal horizon. The breakout strategies default to single US stocks (`donchian_breakout.py:58`) with equity-only asset classes (`base.py:26`). The rule DSL has percentage stops only (`strategies/rules/spec.py`).
- **Spec:**
  - `Momentum`: `lookback_days` 20 → 126 (bounds 63–252), plus `skip_days=21`.
  - `DonchianBreakout` and `TrendlineBreakout`: `applicable_asset_classes=("commodity","crypto","bond")`, with equity opt-in through BL-26's `asset_classes` param; a crypto default ticker; the Grimes evidence in the docstring.
  - Rule DSL `RiskExits`: `trailing_stop_vol_multiple` (0.25–3.0) and `trailing_stop_atr_multiple` (1–6), plus `efficiency_ratio(n=10)` and `kama(10,2,30)` indicators.
  - Backfill `hypothesis`, `alpha_family` and `label_horizon_bars` on every existing example strategy.
- **Seam:** existing classes.
- **Owns:** `strategies/examples/*.py` (existing files only), `strategies/rules/*`.
- **Parallel:** none.
- **Tests:** new defaults; breakouts filtered off equity unless opted in; the trailing stop in the DSL; ER on a straight line is 1; every catalogued strategy has a non-empty hypothesis.
- **Size:** M / S.
- **Deps:** BL-26.

### Advanced (wave 5)

#### BL-44 Covariance estimators and optimising constructors
- **Sources:** López de Prado (*MLAM*: Marchenko-Pastur denoising, HRP, NCO); Boyd (single-period trade optimisation); Pfaff (ERC, most-diversified); Meucci (effective number of bets); Ruppert & Matteson (Ledoit-Wolf).
- **Problem:** there is no covariance estimate or diversification measure anywhere.
- **Spec:**
  - ABC `CovarianceEstimator.estimate(returns) -> ndarray`. Implementations: `sample`, `ledoit_wolf` (sklearn, wrapped), `ewma(λ=0.94)`, `denoised(method="constant_residual")`.
  - Constructors:
    - `hrp`: single linkage and recursive bisection.
    - `erc`: minimise `½yᵀΣy − Σb_i log y_i` with scipy.
    - `mean_variance_costs`, cvxpy wrapped: maximise `μᵀw − γ_risk·wᵀΣw − γ_trade·Σ(a_i|z_i| + b_iσ_i|z_i|^{3/2}/sqrt(V_i/value))`, subject to `1ᵀw ≤ 1`, `0 ≤ w ≤ w_max` and `Σ|z| ≤ τ`.
  - `enb(weights, cov) = exp(−Σp_k ln p_k)` over principal components.
- **Seam:** `CovarianceEstimator` plus `PortfolioConstructor` (registered).
- **Owns:** `portfolio/{covariance,hrp,erc,optimizers,diversification}.py` (new).
- **Parallel:** none within a tick. Constructor hyperparameter sweeps go through lab trials.
- **Tests:** HRP weights sum to 1 on a block matrix; ERC risk contributions are equal; the MV solution respects its constraints; ENB is 1 for one factor and N for identity.
- **Size:** M / M.
- **Deps:** BL-08.

#### BL-45 ML hygiene: purged CV, CPCV and a labelling toolkit
- **Sources:** López de Prado (*AFML* chapters 3, 4, 7, 10, 12); Jansen (nested CV); Shreve (break-even probability).
- **Problem:** `GridTuner` fits and scores on the same train window (`lab/tuning/grid.py:63-65`), so ML strategies are selected on in-sample fit. The meta-label threshold is absolute (0.5).
- **Spec:**
  - `lab/cv.py`: `PurgedKFold(n_splits, embargo_pct=0.01)` and `CombinatorialPurgedKFold(n_groups=6, n_test_groups=2)`, which gives 15 splits and 5 paths.
  - `CVObjective(inner, folds=5)`.
  - Test id `cpcv`: passes when at least 60% of path Sharpes are positive and the pooled PSR is ≥ 0.9.
  - `LabDataset.train_windows` for non-contiguous training.
  - `features/labels.py`: `triple_barrier` (σ from EWMA span 100, high/low touches, t1 kept), `avg_uniqueness`, `sequential_bootstrap`, `bet_size(p) = 2Φ((p−1/K)/sqrt(p(1−p))) − 1`, discretised in steps of 0.1.
  - The `Classifier` seam gets `fit(x, y, sample_weight)`.
  - Meta-label threshold becomes `p* = sl/(tp+sl) + margin`.
- **Seam:** `Objective`, `SurvivalTest`, `Classifier`.
- **Owns:** `lab/cv.py`, `lab/survival/cpcv.py`, `features/labels.py` (new); `features/ml.py`, `strategies/examples/trendline_meta_label.py`, `lab/dataset.py`.
- **Parallel:** CPCV splits (15 tasks, each re-tuned) through `run_tasks`.
- **Tests:** purging removes overlapping samples; the path count; the CPCV split count; bet size is 0 at p = 0.5; the break-even threshold.
- **Size:** H / L.
- **Deps:** BL-07, BL-20.

#### BL-46 Latent regime model and volatility-regime inputs
- **Sources:** Hamilton (Markov switching; use filtered, never smoothed, probabilities); Tsay; Sinclair and Gatheral (VIX term structure, vol regimes); Dixon et al. (HMMs).
- **Problem:** all regime logic uses hard thresholds (`macro_regime.py`, `feature_regime.py`), and no VIX data exists.
- **Spec:**
  - ABC `RegimeModel.fit(returns) / filtered_probs(returns)`.
  - `MarkovSwitchingRegime(k=2, switching_variance=True)` fits with a wrapped `statsmodels` on train data only. Inference is our own numpy Hamilton filter over the stored params (μ_j, σ_j, P).
  - `LatentRegimeFilter` wrapper: risk-off when P(high vol) exceeds the threshold.
  - Yahoo ingests `^VIX` and `^VIX3M` into `macro_indicators` as `vix_spot` and `vix_3m` for `USA`.
  - New regime condition `vix_term_structure(ratio > 1)` registered through BL-42.
- **Seam:** `RegimeModel` plus the condition registry.
- **Owns:** `features/regimes.py`, `strategies/latent_regime.py`, `features/regime_conditions_vix.py` (new); `ingest/sources/yahoo.py`.
- **Parallel:** none.
- **Tests:** filter probabilities on a two-regime simulation; the filter uses no future bars; VIX rows are normalised to the canonical indicator names.
- **Size:** M / M.
- **Deps:** BL-42.

#### BL-47 Live risk monitoring and a correlation-to-pool check
- **Sources:** Danielsson (VaR/ES forecasts, violation ratio); Tulchinsky (alpha decay, pool correlation); Pole; Jansen (FDR across the registry).
- **Problem:** there is no forward-looking risk number and no decay monitor after promotion, and promotion ignores correlation with the live pool.
- **Spec:**
  - A post-tick hook computes one-day 95% and 99% VaR and ES from an EWMA covariance (λ = 0.94, 250 days) and stores them in `risk_snapshots` (migration `011`).
  - `health` warns when the rolling 250-day violation ratio is above 1.5 or below 0.5.
  - The decay monitor alerts when the rolling 60/120-day attributed IR stays below 0 for N days or falls below 50% of the OOS IR.
  - Test id `pool_correlation` fails when the validation-return correlation with any active strategy is above 0.7, unless the candidate's IR is at least 10% higher.
  - `registry audit` applies BH at q = 0.1 to the PSR p-values.
- **Seam:** `PostTickHook` and `SurvivalTest`.
- **Owns:** `production/risk_metrics.py`, `production/decay.py`, `lab/survival/pool_correlation.py`, `store/migrations_sqlite/011_risk_snapshots.sql` (all new).
- **Parallel:** pool backtests as `run_tasks`.
- **Tests:** VaR on a known-σ fixture; violation ratio counts; a decay alert; correlation fail and pass.
- **Size:** M / M.
- **Deps:** BL-12, BL-22.

#### BL-48 Crisis windows, stress simulation and a volatility forecaster
- **Sources:** Kindleberger; Danielsson; Tsay (GARCH-t); Ruppert & Matteson (block bootstrap); Gatheral (HAR, rough-vol-lite); Hull.
- **Problem:** no test looks at named crises or simulated alternative histories.
- **Spec:**
  - Test id `crisis`:
    - windows: GFC 2007-10..2009-03, Euro 2011-05..10, Q4-2018, COVID 2020-02-19..03-23, 2022 H1, crypto 2022-05..11;
    - passes when the maximum drawdown in each window is ≤ 1.5× the benchmark's there;
    - windows without data are skipped and `crisis_coverage` is reported.
  - Test id `stress`: K = 200 alternative validation windows by stationary block bootstrap (block 20) or GARCH-t filtered historical simulation. Passes when the 5th-percentile Sharpe is above −0.5 and the 95th-percentile drawdown is within the limit.
  - ABC `VolForecaster`: `ewma`, `garch(1,1)-t` (wrapped `arch`), `har_rv`.
- **Seam:** `SurvivalTest` and `VolForecaster`.
- **Owns:** `lab/survival/crisis.py`, `lab/survival/stress.py`, `features/vol_forecast.py` (all new).
- **Parallel:** K = 200 stress paths as `run_tasks`, which fills all 32 cores.
- **Tests:** window skipping; a synthetic crash fails; the bootstrap is seeded; GARCH fitting sits behind the seam (no `arch` types leak).
- **Size:** M / M.
- **Deps:** BL-07, BL-22.

#### BL-49 Engineering guards
- **Sources:** McKinney (point-in-time discipline); Slatkin (type checking, property tests); Hilpisch (vectorised screening); Clenow and Malkiel (point-in-time universes).
- **Problem:**
  - Strategies get the full lake (`engine.py:206`, `ranker.py:82`), so nothing enforces point-in-time reads.
  - There is no type checker or property test in CI.
  - `vectorbt` is named in `CLAUDE.md` but unused.
- **Spec:**
  - `store/pit.py` `PointInTimeLake(lake, as_of)`: the getters clamp to `as_of`, statements go by `filing_date`, and raw `sql()` raises unless `allow_raw=True`. The engine and ranker pass it.
  - The engine and ranker honour `universe_membership` (BL-37).
  - A planted-future-bar test runs automatically over every catalogued strategy.
  - Basic-mode pyright on `src/` in CI.
  - Hypothesis property tests: cost monotonicity for every `CostModel`; broker invariants (cash ≥ 0, no shorts); every `RiskRule`; `orders_from_targets` idempotence.
  - Optional `VectorizedStrategy.target_positions(bars_wide, params)` with `lab/vectorized.py` as a tuner pre-screen, plus a parity test against the event engine on a zero-cost fixture.
- **Seam:** a lake proxy, plus CI.
- **Owns:** `store/pit.py`, `lab/vectorized.py` (new); `pyrightconfig.json` (new); `backtest/engine.py`, `production/ranker.py`, `.github/workflows/ci.yml`, `tests/property/*`. The `hypothesis` and `pyright` dev dependencies go in through the integration step.
- **Parallel:** vectorised pre-screen.
- **Tests:** as listed; the PIT test fails if any strategy reads past `as_of`.
- **Size:** H / M.
- **Deps:** BL-12, BL-37.

### Parked (no wave yet)

Each of these is either blocked or not worth building yet:
- Graham screens and net-nets; insider-buying signal. These need paid, deep fundamentals. Revisit after BL-41.
- Ehlers DSP pack (roofing filter, SuperSmoother, dominant cycle) and a roofed-RSI option in `rsi_pca`.
- Fracdiff with a minimum-d ADF search; stationarity and premise test (Chan); Kalman helpers; a Bayesian read-out head for `rsi_pca`.
- Cointegrated pairs. These need shorts, and the broker is long-only.
- Kelly/CPPI leverage governor and the leverage-curve diagnostic (Vince). A cash account caps gross at 1.0; revisit if margin is ever enabled.
- Random-entry baseline test (Davey). MCPT, vs-random and the event study cover most of it.
- T+1 cash settlement; continuous futures and carry. These need contract-level bars.
- Alpaca OPG/CLS auction orders and marketable-limit collars. The owner has deferred Alpaca.
- Anything involving options.

## 3. Ranked list

Ranking is by impact on "the registered strategy has a real, survivable, net-of-cost edge, and we will know when it stops", divided by effort. BL-01 (in progress under 8.6) comes before everything.

Some enablers rank lower on their own (BL-05, BL-10, BL-11). They are still scheduled early, because higher-ranked items depend on them.

| Rank | ID | Item | Impact | Effort | Wave |
|---|---|---|---|---|---|
| 1 | BL-02 | Round-trip trade ledger | H | M | 1 |
| 2 | BL-08 | Portfolio-construction seam and signal normalisation | H | M | 1 |
| 3 | BL-12 | Multi-strategy construction replaces winner-take-all | H | L | 2 |
| 4 | BL-04 | Trial ledger and pre-registration | H | M | 1 |
| 5 | BL-14 | Deflated Sharpe test | H | S | 2 |
| 6 | BL-13 | Realistic costs by default | H | S | 1 (integration) |
| 7 | BL-16 | Statistical OOS gate (PSR, min trades, CI) | H | S | 2 |
| 8 | BL-22 | Benchmark everywhere, benchmark-relative test | H | M | 2 |
| 9 | BL-07 | Parallel lab execution on 32 cores | H | M | 1 |
| 10 | BL-18 | Cost stress and cost budget (speed limit) | H | S | 2 |
| 11 | BL-27 | Risk rules: position risk, portfolio vol, drawdown scaling, liquidity | H | M | 3 |
| 12 | BL-28 | Circuit breaker and operational gating | H | M | 3 |
| 13 | BL-17 | Monte Carlo over trades | H | S | 2 |
| 14 | BL-24 | Promotion governance and intervention log | H | S | 1 |
| 15 | BL-20 | Walk-forward upgrades (embargo, WFE, stitched PSR, parallel folds) | H | M | 2 |
| 16 | BL-05 | Statistics kernel | H | M | 1 |
| 17 | BL-11 | Risk-rule seam | H | M | 1 |
| 18 | BL-19 | Plateau and cross-instrument tests | H | S | 2 |
| 19 | BL-29 | Quit rule | H | S | 3 |
| 20 | BL-25 | Incubation-grade go-live | H | S | 2 |
| 21 | BL-38 | QuantMomentum | H | M | 4 |
| 22 | BL-15 | PBO via CSCV | H | M | 2 |
| 23 | BL-34 | Event study against baseline drift | H | S | 3 |
| 24 | BL-42 | Composite k-of-n RegimeFilter | H | M | 4 |
| 25 | BL-39 | Stocks on the Move and ATR-parity constructor | H | M | 4 |
| 26 | BL-40 | Cross-asset trend (EWMAC, TSMOM, ATH) and trailing stop | H | M | 4 |
| 27 | BL-30 | Participation cap, partial fills, limit and stop fills | H | M | 3 |
| 28 | BL-35 | Vs-random test | H | M | 3 |
| 29 | BL-37 | Point-in-time universes and lab preflight | H | M | 3 |
| 30 | BL-49 | Engineering guards (PIT lake, pyright, Hypothesis, vectorised pre-screen) | H | M | 5 |
| 31 | BL-41 | QuantValue with forensic screens | H | L | 4 |
| 32 | BL-10 | Survival-test registry and presets | M | S | 1 |
| 33 | BL-03 | Richer, correct metrics | M | S | 1 |
| 34 | BL-09 | Volatility toolkit and cross-sectional helpers | M | S | 1 |
| 35 | BL-21 | OOS windows for every test, MCPT n=200 | M | S | 2 |
| 36 | BL-26 | Strategy metadata | M | S | 1 |
| 37 | BL-06 | Reproducibility manifest | M | S | 1 |
| 38 | BL-23 | Market-beta attribution | M | S | 2 |
| 39 | BL-43 | Legacy defaults fix and metadata backfill | M | S | 4 |
| 40 | BL-32 | TCA and decision journal | M | M | 3 |
| 41 | BL-33 | Signal IC analysis | M | M | 3 |
| 42 | BL-31 | Volatility-aware impact and per-ticker spreads | M | M | 3 |
| 43 | BL-36 | Statement audit | M | M | 3 |
| 44 | BL-45 | Purged CV, CPCV, labelling toolkit | H | L | 5 |
| 45 | BL-44 | Covariance estimators and optimising constructors | M | M | 5 |
| 46 | BL-47 | Live risk monitoring and pool correlation | M | M | 5 |
| 47 | BL-48 | Crisis windows, stress simulation, VolForecaster | M | M | 5 |
| 48 | BL-46 | Latent regime model and VIX inputs | M | M | 5 |

## 4. Execution plan

### Preconditions
- The in-progress work must merge first: 8.2, 8.5, 8.6, 6.6 and 6.7. Wave 1 builds on 8.6's adjusted prices, 8.2's tick costs and 6.6's `lab/parallel.py`.
- 5.2 (the Angular console) can keep running alongside, because no Phase 9 package touches `web/`.

### Rules for every wave
- At most 6 packages per wave, one agent per package, each on its own branch or worktree.
- File ownership inside a wave is disjoint. Each package also owns the new test files for its modules (`tests/unit/test_<module>.py`, `tests/integration/test_<module>.py`). Shared fixtures go in a new `tests/fixtures/<package>.py`, never in `tests/conftest.py`.
- Code in one package that needs another package in the **same** wave uses only duck-typed hooks with defaults (`getattr(dataset, "benchmark", "auto")`, `getattr(tuned, "trials", None)`). So any merge order works, and each package's tests pass on their own.
- After each wave comes the integration step: merge, wire the hotspots listed in §1 rule 3, review the combined diff, fix confirmed findings, run `uv run pytest -n auto`, ruff and ruff format.

### Wave 1: foundations

| WP | Items | Scope | Owns |
|---|---|---|---|
| W1.1 | BL-02, BL-03 | Trade ledger, trade stats, corrected metrics on `BacktestReport` | `backtest/trades.py`, `backtest/metrics.py`, `backtest/report.py`, `backtest/simulated_broker.py`, `lab/backtesting.py` |
| W1.2 | BL-04, BL-05, BL-06 | Trial ledger with pre-registration, `stonks.stats` kernel, reproducibility manifest | `lab/runner.py`, `lab/trials.py`, `lab/manifest.py`, `src/stonks/stats/*`, `registry/artifact.py`, `store/migrations_sqlite/006_lab_trials.sql` |
| W1.3 | BL-07 | Extend 6.6's pool to tuners; snapshots; read-only lake; `TrialOutcome` | `lab/parallel.py`, `lab/tuning/base.py`, `lab/tuning/grid.py`, `lab/tuning/random.py`, `lab/objectives.py`, `core/protocols.py`, `store/lake.py` |
| W1.4 | BL-08, BL-09 | `PortfolioConstructor` seam, signal normalisation, buffered order diff, volatility and cross-section helpers | `src/stonks/portfolio/{__init__,base,signals,constructors,orders}.py`, `features/volatility.py`, `features/cross_section.py` |
| W1.5 | BL-10, BL-11 | Survival-test registry and presets; `RiskRule` registry and `RiskContext` | `lab/survival/registry.py`, `app/lab.py`, `production/risk.py`, `production/prices.py`, `production/rules/{__init__,caps}.py` |
| W1.6 | BL-24, BL-26 | Status-change audit and promotion rules; strategy metadata hooks | `store/migrations_sqlite/007_status_changes.sql`, `registry/store.py`, `app/strategies.py`, `strategies/base.py` |

Integration 1:
- BL-13: realistic `[backtest.costs]` defaults, plus a zero-cost warning.
- `[lab.parallel]` settings.
- CLI: `lab run --hypothesis/--premortem/--preset/--workers`; `_LAB_TESTS` (`cli.py:947`) replaced by the registry.
- `registry promote/retire --reason/--override` across the CLI, API and MCP.
- `scipy` added explicitly to `pyproject.toml`.

### Wave 2: validation gates and construction wiring

| WP | Items | Scope | Owns |
|---|---|---|---|
| W2.1 | BL-12 | One construction pipeline for the tick and the backtest; attribution; post-tick hook registry | `production/tick.py`, `production/ranker.py`, `production/shadow.py`, `production/hooks.py`, `portfolio/pipeline.py`, `backtest/engine.py`, `store/migrations_sqlite/008_position_attribution.sql` |
| W2.2 | BL-14, BL-15, BL-16 | Deflated Sharpe, PBO, PSR-based OOS gate with trade minimum | `lab/survival/deflated_sharpe.py`, `lab/survival/pbo.py`, `lab/survival/oos.py` |
| W2.3 | BL-17, BL-18, BL-19 | Monte Carlo over trades, cost stress and budget, plateau and cross-instrument | `lab/survival/mc_trades.py`, `lab/survival/cost_stress.py`, `lab/survival/plateau.py`, `lab/survival/cross_instrument.py` |
| W2.4 | BL-20, BL-21 | Embargo, walk-forward efficiency and stitching, parallel folds; validation windows everywhere; MCPT n=200 | `lab/dataset.py`, `lab/survival/walk_forward.py`, `lab/survival/perturbation.py`, `lab/survival/runs_test.py`, `lab/survival/period_stability.py`, `lab/survival/permutation.py` |
| W2.5 | BL-22, BL-23 | Benchmark curve and stats, beta attribution, `benchmark_relative` test, report tear sheet | `backtest/benchmark.py`, `lab/backtesting.py`, `lab/survival/benchmark_relative.py`, `reporting/*` |
| W2.6 | BL-25 | Incubation-grade go-live with MinTRL, Monte Carlo band and a promotion checklist | `production/golive.py` |

Integration 2: `[production.construction]`, new `[golive]` defaults, the `promotion` preset made the default for registering runs, and the report CLI options.

### Wave 3: risk, execution realism, research tools, data

| WP | Items | Scope | Owns |
|---|---|---|---|
| W3.1 | BL-27 | Six new risk rules | `production/rules/{risk_per_position,portfolio_vol,drawdown_scaling,liquidity,sector_cap,max_holding}.py` |
| W3.2 | BL-28, BL-29 | Circuit breaker, operational halt, logged reset, quit rule | `production/rules/{circuit_breaker,operational_halt}.py`, `production/halts.py`, `production/quit_rule.py`, `production/health.py`, `store/migrations_sqlite/009_risk_halts.sql` |
| W3.3 | BL-30, BL-31 | Participation cap, partial fills, limit and stop fills, gap guard; volatility-aware impact; per-ticker spreads | `backtest/fills.py`, `backtest/simulated_broker.py`, `backtest/engine.py`, `backtest/costs.py`, `features/spread.py` |
| W3.4 | BL-32 | Decision price and context, implementation shortfall, journal | `core/types.py`, `store/migrations_sqlite/010_tca.sql`, `production/tca.py`, `production/tick.py`, `execution/reconcile.py` |
| W3.5 | BL-33, BL-34, BL-35 | Signal IC, event study, vs-random | `lab/signal_eval.py`, `lab/survival/event_study.py`, `lab/survival/vs_random.py` |
| W3.6 | BL-36, BL-37 | Statement audit, point-in-time universes, lab preflight | `store/audit.py`, `store/migrations_duckdb/011_statement_flags.sql`, `store/migrations_duckdb/012_universe_membership.sql`, `store/lake.py`, `lab/universe.py`, `lab/preflight.py`, `lab/runner.py` |

Integration 3: rule, breaker and fill-model settings; the commands `stonks risk reset --reason`, `stonks tca`, `stonks journal`, `stonks data audit` and `stonks lab ic`; API routes for TCA and halts.

### Wave 4: strategies built on the new seams

| WP | Items | Scope | Owns |
|---|---|---|---|
| W4.1 | BL-38 | QuantMomentum (12-2, top decile, frog-in-the-pan, quarterly) | `strategies/examples/quant_momentum.py`, `features/momentum.py` |
| W4.2 | BL-39 | Stocks on the Move and the `atr_parity` constructor | `strategies/examples/stocks_on_the_move.py`, `features/trend.py`, `portfolio/atr_parity.py` |
| W4.3 | BL-40 | EWMAC, time-series momentum, all-time-high trend, `TrailingStopWrapper` | `strategies/examples/{ewmac_trend,time_series_momentum,ath_trend}.py`, `strategies/trailing_stop.py`, `features/trend_following.py` |
| W4.4 | BL-41 | QuantValue with forensic screens; Piotroski and magic-formula modes | `strategies/examples/quant_value.py`, `features/fundamentals.py`, `store/lake.py` |
| W4.5 | BL-42 | Composite k-of-n `RegimeFilter` and condition registry | `strategies/regime.py`, `features/regime_conditions.py` |
| W4.6 | BL-43 | Momentum 12-1 default; breakouts moved off single stocks; vol-scaled DSL stops; metadata backfill | existing `strategies/examples/*.py` (not the W4.1-W4.4 files), `strategies/rules/*` |

Integration 4: add `RegimeFilter` and `TrailingStopWrapper` to the catalog's `_WRAPPERS`. Add composition tests: `RegimeFilter(price_trend(SPY.US, 200), block_new_buys)` around Stocks on the Move and QuantMomentum. Run each new strategy through the `promotion` preset once on the fixture lake.

### Wave 5: advanced

| WP | Items | Scope | Owns |
|---|---|---|---|
| W5.1 | BL-44 | Covariance estimators, HRP, ERC, mean-variance with costs, ENB | `portfolio/{covariance,hrp,erc,optimizers,diversification}.py` |
| W5.2 | BL-45 | Purged and combinatorial CV, CPCV test, triple-barrier and uniqueness toolkit, bet sizing | `lab/cv.py`, `lab/survival/cpcv.py`, `features/labels.py`, `features/ml.py`, `strategies/examples/trendline_meta_label.py`, `lab/dataset.py` |
| W5.3 | BL-46 | Markov-switching regime filter and VIX term-structure condition | `features/regimes.py`, `strategies/latent_regime.py`, `features/regime_conditions_vix.py`, `ingest/sources/yahoo.py` |
| W5.4 | BL-47 | Live VaR/ES and violation ratio, alpha-decay monitor, pool-correlation test | `production/risk_metrics.py`, `production/decay.py`, `lab/survival/pool_correlation.py`, `store/migrations_sqlite/011_risk_snapshots.sql` |
| W5.5 | BL-48 | Crisis windows, stress simulation, `VolForecaster` | `lab/survival/crisis.py`, `lab/survival/stress.py`, `features/vol_forecast.py` |
| W5.6 | BL-49 | Point-in-time lake proxy, universe membership in engine and ranker, pyright, Hypothesis, vectorised pre-screen | `store/pit.py`, `lab/vectorized.py`, `pyrightconfig.json`, `backtest/engine.py`, `production/ranker.py`, `.github/workflows/ci.yml`, `tests/property/*` |

Integration 5: `statsmodels`, `arch` and `cvxpy` dependencies (each wrapped); `hypothesis` and `pyright` dev dependencies; add the pool-correlation, crisis and CPCV tests to the `promotion` preset.

### Where the 32 cores go

Everything goes through `lab/parallel.run_tasks` (BL-07), with deterministic seeds per task and one read-only DuckDB snapshot connection per worker.

| Work | Tasks per run | Item |
|---|---|---|
| Tuning trials (grid or random) | budget (20-1,000) | BL-07 |
| MCPT permutations (re-tune mode is the costliest) | 200 | BL-21, 6.6 |
| Walk-forward folds × matrix cells | 4-72 | BL-20 |
| Vs-random noise lakes, each a full tune | 20 | BL-35 |
| Stress paths | 200 | BL-48 |
| CPCV splits, each re-tuned | 15 | BL-45 |
| Plateau neighbours, per-ticker consistency | 2k + N | BL-19 |
| Cost multipliers, pool backtests | 4-10 | BL-18, BL-47 |
| IC dates and event extraction by ticker | chunks | BL-33, BL-34 |
| Tick scoring per strategy and ticker chunk | when the universe exceeds 500 | BL-12 |

Monte Carlo over trades (BL-17), CSCV (BL-15) and the bootstrap (BL-05) are vectorised numpy and need no processes.
