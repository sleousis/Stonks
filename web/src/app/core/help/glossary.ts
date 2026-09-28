// Plain-English, one-line explanations of every figure and trading word the
// console shows. The single source for in-app help: <app-help-tip>, stat
// tiles and table headers look terms up here by key or by their visible
// label. Every tip links to the in-app glossary page (/help/glossary), built
// from this file.
//
// Add a metric: add a key to METRIC_KEYS (or a trading word to TRADING_KEYS),
// an entry below (the compiler insists), and any labels or API keys it
// appears under as `aliases`.

/** The in-app glossary page. Each term has an anchor named after its key. */
export const GLOSSARY_PATH = '/help/glossary';

/** `/help/glossary#max_drawdown`, or the page itself without a key. */
export function glossaryUrl(key?: string): string {
  return key ? `${GLOSSARY_PATH}#${key}` : GLOSSARY_PATH;
}

export const METRIC_KEYS = [
  // Returns
  'total_return',
  'cagr',
  'equity',
  'volatility',
  // Risk-adjusted
  'sharpe',
  'deflated_sharpe',
  'probabilistic_sharpe',
  'sortino',
  'calmar',
  'information_ratio',
  // Losses and tail risk
  'drawdown',
  'max_drawdown',
  'var',
  'expected_shortfall',
  // Trades
  'profit_factor',
  'expectancy',
  'win_rate',
  'trades',
  'turnover',
  'exposure',
  'slippage',
  'basis_points',
  // Benchmark-relative
  'alpha',
  'beta',
  'tracking_error',
  // Overfitting and validation
  'pbo',
  'min_trl',
  'p_value',
  'is_score',
  'oos_score',
  'best_score',
  'out_of_sample',
  'walk_forward',
  'period_stability',
  'perturbation',
  'drift',
  'psi',
  'runs_test',
  'permutation',
  // Added with Phase 9 (trade stats, benchmark, overfitting evidence)
  'ulcer_index',
  'drawdown_duration',
  'payoff_ratio',
  'holding_time',
  'cost_drag',
  'excess_cagr',
  'capture_ratio',
  'benchmark',
  'trials',
  'wfe',
  'mc_drawdown_band',
  'risk_of_ruin',
  'cost_stress',
  'break_even_costs',
  'plateau',
  'cross_instrument',
  'benchmark_relative',
  'embargo',
  'hypothesis',
  'premortem',
  'label_horizon',
  // Signal research
  'information_coefficient',
  // Live risk and portfolio insights
  'violation_ratio',
  'alpha_decay',
  'concentration',
  // Returns with deposits and withdrawals taken out
  'twr',
  'mwr',
] as const;

export type MetricKey = (typeof METRIC_KEYS)[number];

/**
 * The words a trader meets in the first hour (UX-42): the trading day, the
 * modes and the safety controls. Each says the system's own name too
 * ("also called ..."), so a trader and an operator mean the same thing.
 */
export const TRADING_KEYS = [
  'paper_trading',
  'live',
  'signals_only',
  'auto',
  'approve',
  'kill_switch',
  'buys_only',
  'circuit_breaker',
  'dry_run',
  'trading_run',
  'fill',
  'universe',
  'recovery_code',
] as const;

export type TradingKey = (typeof TRADING_KEYS)[number];

/**
 * The product itself (F5): what a strategy, a follow and a portfolio are,
 * the three ladders of docs/design/vocabulary.md (a strategy's status, how
 * you follow it, your portfolio's stage), one set of words each, and the
 * pages and things a trader meets around them.
 */
export const PRODUCT_KEYS = [
  'strategy',
  'follow',
  'portfolio',
  'draft',
  'on_trial',
  'approved',
  'retired',
  'test_book',
  'go_live_check',
  'strategy_review',
  'trial_results',
  'robustness_tests',
  'verdict',
  'portfolio_stage',
  'simulated',
  'broker_paper',
  'real_money_small',
  'real_money_full',
  'strategy_part',
  'signal',
  'watchlist',
  'screener',
  'trade_costs',
] as const;

export type ProductKey = (typeof PRODUCT_KEYS)[number];
export type GlossaryKey = MetricKey | TradingKey | ProductKey;

/** Every key: product words, then trading words, then figures. */
export const GLOSSARY_KEYS: readonly GlossaryKey[] = [
  ...PRODUCT_KEYS,
  ...TRADING_KEYS,
  ...METRIC_KEYS,
];

/** The glossary page's sections, in order. */
export const GLOSSARY_GROUPS: readonly { title: string; keys: readonly GlossaryKey[] }[] = [
  { title: 'How Stonks works', keys: PRODUCT_KEYS },
  { title: 'Trading words', keys: TRADING_KEYS },
  { title: 'Figures', keys: METRIC_KEYS },
];

export interface GlossaryEntry {
  /** Display name, as a trader would say it. */
  term: string;
  /** One plain-English sentence (two at most). */
  short: string;
  /** Other labels or API keys this term appears under. Matched loosely. */
  aliases?: readonly string[];
}

export const GLOSSARY: Record<GlossaryKey, GlossaryEntry> = {
  // Trading words ------------------------------------------------------------
  paper_trading: {
    term: 'Paper',
    short:
      'A way to follow a strategy: it trades your paper portfolio with pretend money on real prices. No real money moves.',
    aliases: ['Paper trading', 'Paper mode', 'Paper portfolio', 'Paper days', 'Practice'],
  },
  live: {
    term: 'Real money',
    short:
      'Orders go to your broker and fill for real. Only a portfolio at a real-money stage trades real money. Also called live.',
    aliases: ['Live', 'Go live', 'Live trading'],
  },
  signals_only: {
    term: 'Alerts only',
    short:
      'A way to follow a strategy: you get an alert for each trade it wants and nothing trades. Also called notify.',
    aliases: ['Signals only', 'Notify', 'Signals only mode'],
  },
  auto: {
    term: 'Automatic',
    short:
      'A way to follow a strategy: its trades go to your broker without asking. It unlocks after enough paper days.',
    aliases: ['Auto', 'Auto mode'],
  },
  approve: {
    term: 'Approve each trade',
    short:
      'A way to follow a strategy: each trade waits for you as a ticket after the trading run. You approve it with a code before the open.',
    aliases: ['Approve mode', 'Approvals', 'Order ticket', 'Order tickets'],
  },
  kill_switch: {
    term: 'Kill switch',
    short:
      'Stops new orders at once, for one portfolio, all yours or everyone. No position is closed. Also called a halt.',
    aliases: ['Stop trading', 'Halt', 'Halts'],
  },
  buys_only: {
    term: 'Stop new buys only',
    short:
      'A kill switch that stops new buys but lets sells and exits go out, so you can still leave positions.',
    aliases: ['Buys only', 'New buys'],
  },
  circuit_breaker: {
    term: 'Circuit breaker',
    short:
      'Halts trading by itself after a big weekly or monthly loss or a deep drawdown, until someone clears it.',
    aliases: ['Breaker', 'Drawdown breaker', 'Loss breaker'],
  },
  dry_run: {
    term: 'Dry run',
    short:
      'A practice trading run: strategies decide and orders are sized, but nothing goes to the broker.',
    aliases: ['Dry-run', 'Rehearsal'],
  },
  trading_run: {
    term: 'Trading run',
    short:
      'The moment each day when strategies read prices, decide and send orders. Also called a tick.',
    aliases: ['Tick', 'Trading runs'],
  },
  fill: {
    term: 'Fill',
    short: 'An order that was carried out: the shares, price and time the broker traded.',
    aliases: ['Fills', 'Filled'],
  },
  universe: {
    term: 'Universe',
    short: 'The list of tickers a strategy may trade, such as an index or your own list.',
    aliases: ['Universes'],
  },
  recovery_code: {
    term: 'Recovery code',
    short:
      'A one-time code that signs you in if you lose your authenticator app. Keep them somewhere safe.',
    aliases: ['Recovery codes'],
  },
  // How Stonks works -----------------------------------------------------------
  strategy: {
    term: 'Strategy',
    short: 'A set of rules that looks at prices after each close and decides what to buy and sell.',
    aliases: ['Strategies'],
  },
  follow: {
    term: 'Follow',
    short:
      'Linking a strategy to one of your portfolios: Alerts only, Paper, Approve each trade or Automatic. Also called a subscription.',
    aliases: ['Following', 'Follows', 'Subscription', 'Subscriptions', 'Follow mode'],
  },
  portfolio: {
    term: 'Portfolio',
    short:
      'Your cash and holdings in one place: a paper portfolio in Stonks or an account at your broker.',
    aliases: ['Portfolios'],
  },
  draft: {
    term: 'Draft',
    short: 'A strategy being built in the Studio. The system has not tested it yet.',
    aliases: ['Drafts', 'Strategy draft'],
  },
  on_trial: {
    term: 'On trial',
    short:
      'A strategy status: the system trades it on its own test book every run, with no real orders. Nobody can follow it for trades yet. Also called shadow.',
    aliases: ['Shadow', 'Trial', 'Put on trial', 'Back on trial'],
  },
  approved: {
    term: 'Approved',
    short:
      'A strategy status: it passed the go-live check, so people can follow it. It does not mean real money. Also called active.',
    aliases: ['Active', 'Approve a strategy', 'Approved strategy'],
  },
  retired: {
    term: 'Retired',
    short:
      'A strategy status: it no longer decides anything. Its history stays, and positions it holds are not closed.',
    aliases: ['Retire'],
  },
  test_book: {
    term: 'Test book',
    short:
      "A strategy's own practice portfolio, run by the system while it is on trial, so its record never depends on anyone following it. Also called a model book or shadow book.",
    aliases: ['Model book', 'Shadow book', 'Paper book', 'Test books'],
  },
  go_live_check: {
    term: 'Go-live check',
    short:
      'The checks a strategy on trial must pass before it can be approved: enough days and trades, small drops, robustness tests.',
    aliases: ['Go-live', 'Go-live gate', 'Go-live checks'],
  },
  strategy_review: {
    term: 'Strategy review',
    short:
      'The admin page listing strategies on trial and whether each passes the go-live check and is ready to approve.',
    aliases: ['Go-live review'],
  },
  trial_results: {
    term: 'Trial results',
    short: 'How strategies do on their own test books, next to your portfolio over the same days.',
  },
  robustness_tests: {
    term: 'Robustness tests',
    short:
      'Checks that a backtest was not luck: other periods, other tickers, higher costs and shuffled data. Also called survival tests.',
    aliases: ['Robustness', 'Robustness test', 'Survival tests', 'Full robustness tests'],
  },
  verdict: {
    term: 'Verdict',
    short:
      'One plain answer per strategy: Worth following, Promising, needs more data, or Not good enough yet.',
    aliases: ['Worth following', 'Promising, needs more data', 'Not good enough yet'],
  },
  portfolio_stage: {
    term: 'Portfolio stage',
    short:
      'How far a portfolio is on the way to real money, from Simulated to Real money, full. Only the stage decides if real money moves.',
    aliases: ['Stage', 'Live stage'],
  },
  simulated: {
    term: 'Simulated',
    short: 'A portfolio stage: Stonks fills orders itself. Nothing reaches a broker. No real money.',
    aliases: ['Simulated paper'],
  },
  broker_paper: {
    term: 'Broker paper',
    short: "A portfolio stage: orders go to the broker's paper account. No real money.",
  },
  real_money_small: {
    term: 'Real money, small',
    short:
      'A portfolio stage: real money, with the allocation you set and tight caps on each order.',
    aliases: ['Live small', 'Live, small'],
  },
  real_money_full: {
    term: 'Real money, full',
    short: 'A portfolio stage: real money at your full allocation.',
    aliases: ['Live scaled', 'Live scale', 'Live, scaled'],
  },
  strategy_part: {
    term: "The strategy's part of your portfolio",
    short:
      'The share of a portfolio one followed strategy trades. Its results are counted apart. Also called a sleeve.',
    aliases: ['Sleeve', 'Strategy sleeve', 'Strategy sleeves'],
  },
  signal: {
    term: 'Signal',
    short:
      'What a strategy wants after a close: buy, sell or hold a ticker, with how strongly it thinks so.',
    aliases: ['Signals', 'Signal score'],
  },
  watchlist: {
    term: 'Watchlist',
    short: 'Your own list of tickers to keep an eye on. Today and the calendar can show only them.',
    aliases: ['Watchlists'],
  },
  screener: {
    term: 'Screener',
    short: 'Filters every ticker by price, returns, size or value and lists the ones that pass.',
    aliases: ['Screen', 'Saved screen'],
  },
  trade_costs: {
    term: 'Trade costs',
    short:
      'What trading really cost: fees, the spread and how far the fill price moved from the decision price.',
    aliases: ['Trade cost', 'Shortfall'],
  },
  // Figures ------------------------------------------------------------------
  total_return: {
    term: 'Total return',
    short: 'How much the value grew or shrank over the whole period, as a percentage.',
    aliases: ['Return', 'final_return', 'Final return'],
  },
  cagr: {
    term: 'CAGR',
    short:
      'Compound annual growth rate: the steady yearly growth that would turn the start value into the end value.',
    aliases: ['Compound annual growth rate', 'cagr_oos', 'Annual return'],
  },
  equity: {
    term: 'Equity',
    short: 'Total value of the account: cash plus everything it holds at current prices.',
    aliases: ['Total value', 'Portfolio value'],
  },
  volatility: {
    term: 'Volatility',
    short: 'How much returns swing around their average, scaled to a year. Higher means bumpier.',
    aliases: ['Annualized volatility', 'Annualised volatility', 'vol'],
  },
  sharpe: {
    term: 'Sharpe ratio',
    short:
      'Return earned per unit of volatility, per year. Above 1 is good, above 2 is rare and worth doubting.',
    aliases: ['Sharpe', 'sharpe_ratio', 'sharpe_oos'],
  },
  deflated_sharpe: {
    term: 'Deflated Sharpe ratio',
    short:
      'The chance the Sharpe ratio is real skill after allowing for how many variants were tried. Near 1 is convincing.',
    aliases: ['Deflated Sharpe', 'DSR', 'dsr'],
  },
  probabilistic_sharpe: {
    term: 'Probabilistic Sharpe ratio',
    short:
      'The chance the true Sharpe ratio is above zero, given how long and how lopsided the record is.',
    aliases: ['Probabilistic Sharpe', 'PSR', 'psr', 'psr0'],
  },
  sortino: {
    term: 'Sortino ratio',
    short: 'Like Sharpe, but only counts downside swings, so big up days are not penalised.',
    aliases: ['Sortino', 'sortino_ratio'],
  },
  calmar: {
    term: 'Calmar ratio',
    short: 'Yearly growth (CAGR) divided by the worst drawdown: growth earned per unit of pain.',
    aliases: ['Calmar', 'calmar_ratio'],
  },
  information_ratio: {
    term: 'Information ratio',
    short:
      'Extra return over the benchmark divided by tracking error: how consistently it beats the benchmark.',
    aliases: ['IR', 'Info ratio'],
  },
  drawdown: {
    term: 'Drawdown',
    short: 'How far value has fallen below its previous peak.',
    aliases: ['Current drawdown'],
  },
  max_drawdown: {
    term: 'Max drawdown',
    short:
      'The largest fall from a peak to a later low: the worst loss you would have sat through.',
    aliases: ['Maximum drawdown', 'MDD', 'Worst drawdown'],
  },
  var: {
    term: 'Value at risk (VaR)',
    short:
      'A loss that should be beaten only on the worst days; 95% VaR is exceeded about one day in twenty.',
    aliases: ['VaR', 'Value at risk', 'var_95', 'VaR 95%'],
  },
  expected_shortfall: {
    term: 'Expected shortfall (ES)',
    short: 'The average loss on the days that are worse than VaR. Also called CVaR.',
    aliases: ['ES', 'CVaR', 'Conditional VaR', 'es_95', 'cvar', 'ES 95%'],
  },
  profit_factor: {
    term: 'Profit factor',
    short:
      'Money made on winners divided by money lost on losers. Above 1 makes money; 1.5 is solid.',
  },
  expectancy: {
    term: 'Expectancy',
    short: 'The average profit or loss per trade, winners and losers together.',
    aliases: ['Avg trade', 'Average trade'],
  },
  win_rate: {
    term: 'Win rate',
    short: 'Share of trades that made money. A low win rate can still pay if winners are big.',
    aliases: ['Hit rate', 'Win %'],
  },
  trades: {
    term: 'Trades',
    short:
      'Number of round trips (buy then sell). Few trades make every other figure less certain.',
    aliases: ['Trade count', 'n_trades', 'trade_count'],
  },
  turnover: {
    aliases: ['turnover_annual', 'Turnover per year'],
    term: 'Turnover',
    short: 'How much of the portfolio is bought and sold per year. More turnover means more costs.',
  },
  exposure: {
    term: 'Exposure',
    short: 'Share of the account invested rather than held as cash.',
    aliases: ['Gross exposure', 'Net exposure', 'time_in_market'],
  },
  slippage: {
    term: 'Slippage',
    short: 'Paying a worse price than expected when an order fills.',
  },
  basis_points: {
    term: 'Basis point (bps)',
    short: 'One hundredth of a percent: 25 bps is 0.25%.',
    aliases: ['bps', 'Basis points'],
  },
  alpha: {
    term: 'Alpha',
    short: 'Return the market does not explain: what is left after accounting for beta.',
    aliases: ['Jensen alpha', 'alpha_annual', 'Alpha per year'],
  },
  beta: {
    term: 'Beta',
    short:
      'How much it moves with the benchmark: 1 moves in step, 0 ignores it, above 1 exaggerates it.',
  },
  tracking_error: {
    term: 'Tracking error',
    short: 'How far returns stray from the benchmark, scaled to a year.',
    aliases: ['TE'],
  },
  pbo: {
    term: 'Probability of backtest overfitting (PBO)',
    short:
      'The chance the best backtest variant does worse than average on new data. Lower is better; above 0.5 is a red flag.',
    aliases: ['PBO', 'Overfitting probability'],
  },
  min_trl: {
    term: 'Minimum track record length (MinTRL)',
    short: 'How long a record must run before its Sharpe ratio can be trusted.',
    aliases: ['MinTRL', 'Min track record', 'mintrl', 'min_track_record_length'],
  },
  p_value: {
    term: 'p-value',
    short:
      'The chance of a result this good from luck alone. Smaller is stronger evidence; under 0.05 is the usual bar.',
    aliases: ['p value'],
  },
  is_score: {
    term: 'In-sample score',
    short:
      'The objective on the data the tuner used to pick parameters. Always looks better than reality.',
  },
  oos_score: {
    term: 'Out-of-sample score',
    short: 'The objective on data the tuner never saw: the honest number.',
  },
  best_score: {
    term: 'Best score',
    short: 'The best objective value the tuner found (for example Sharpe), before survival tests.',
  },
  out_of_sample: {
    term: 'Out of sample (OOS)',
    short:
      'Tested on data that was held back from tuning, to catch strategies that only fit the past.',
    aliases: ['OOS', 'oos', 'Out-of-sample'],
  },
  walk_forward: {
    term: 'Walk-forward',
    short: 'Tune on the past, test on the next slice, roll forward and repeat.',
    aliases: ['Walk forward'],
  },
  period_stability: {
    term: 'Period stability',
    short: 'Checks the strategy works in each sub-period, not just in one lucky stretch.',
  },
  perturbation: {
    term: 'Perturbation',
    short: 'Adds small noise to prices and parameters to see if the results hold.',
  },
  drift: {
    term: 'Drift',
    short: 'A change in how the data behaves between the training period and later ones.',
  },
  psi: {
    term: 'Population stability index (PSI)',
    short: 'How much a distribution shifted. Under 0.1 is stable; over 0.25 is a big shift.',
    aliases: ['PSI'],
  },
  runs_test: {
    term: 'Runs test',
    short: 'Checks whether wins and losses cluster together more than chance would.',
  },
  permutation: {
    term: 'Monte Carlo permutation test (MCPT)',
    short: 'Shuffles the price history many times and checks the real result beats most shuffles.',
    aliases: ['MCPT', 'Monte Carlo permutation', 'Permutation test'],
  },
  ulcer_index: {
    term: 'Ulcer index',
    short: 'Pain from drawdowns: how deep and how long value sat below its peak. Lower is calmer.',
    aliases: ['Ulcer', 'ulcer'],
  },
  drawdown_duration: {
    term: 'Drawdown duration',
    short: 'The longest stretch, in bars, spent below a previous peak before recovering.',
    aliases: ['Longest drawdown', 'max_dd_duration_bars', 'Max drawdown duration'],
  },
  payoff_ratio: {
    term: 'Payoff ratio',
    short: 'The average winning trade divided by the average losing trade.',
    aliases: ['Payoff', 'Win/loss ratio'],
  },
  holding_time: {
    term: 'Holding time',
    short: 'How many bars a trade stays open on average.',
    aliases: ['Avg holding time', 'avg_bars_held', 'Average holding time'],
  },
  cost_drag: {
    term: 'Cost drag',
    short: 'Return lost to fees, spread and market impact each year.',
    aliases: ['cost_drag_annual', 'Cost drag per year', 'Costs'],
  },
  excess_cagr: {
    term: 'Excess CAGR',
    short:
      "Yearly growth above the benchmark's. Positive means it beat simply holding the benchmark.",
    aliases: ['Excess return'],
  },
  capture_ratio: {
    term: 'Capture ratio',
    short:
      "Up capture: share of the benchmark's rises it caught. Down capture: share of its falls. Want high up, low down.",
    aliases: ['Up capture', 'Down capture', 'up_capture', 'down_capture', 'Capture'],
  },
  benchmark: {
    term: 'Benchmark',
    short:
      'What the result is compared with: an index such as SPY, the equal-weight universe (EW), or nothing.',
    aliases: ['Benchmark comparison'],
  },
  trials: {
    term: 'Trials',
    short:
      'Parameter sets tried. The more tried, the likelier a good score is luck, so it is always shown.',
    aliases: [
      'n_trials',
      'n_trials_run',
      'n_trials_class',
      'trials_class',
      'Trials this run',
      'Trials of this class',
    ],
  },
  wfe: {
    term: 'Walk-forward efficiency (WFE)',
    short:
      'Out-of-sample result divided by in-sample result. Near 1 holds up; under 0.5 mostly fitted noise.',
    aliases: ['WFE', 'Walk-forward efficiency'],
  },
  mc_drawdown_band: {
    term: 'Monte Carlo drawdown band',
    short:
      "The drawdown range seen when the backtest's trades are reshuffled thousands of times; p95 is a bad but plausible case.",
    aliases: ['p95_max_dd', 'median_max_dd', 'MC drawdown', 'mc_trades', 'Monte Carlo trades'],
  },
  risk_of_ruin: {
    term: 'Risk of ruin',
    short: 'Share of reshuffled trade paths that hit the ruin drawdown (40% by default).',
    aliases: ['Ruin'],
  },
  cost_stress: {
    term: 'Cost stress',
    short: 'Re-runs the backtest at two and three times the costs to see if the edge survives.',
    aliases: ['Cost stress test'],
  },
  break_even_costs: {
    term: 'Break-even cost multiple',
    short: "How many times today's costs the strategy can pay before it stops making money.",
    aliases: ['break_even_multiple', 'Break even'],
  },
  plateau: {
    term: 'Plateau',
    short:
      'Checks that nearby parameter values also work, so the best result is a plateau, not a lone spike.',
    aliases: ['Parameter plateau'],
  },
  cross_instrument: {
    term: 'Cross-instrument',
    short:
      'Runs the same strategy on each ticker and on held-out ones; an edge should not live in one name.',
    aliases: ['Cross instrument'],
  },
  benchmark_relative: {
    term: 'Benchmark-relative test',
    short: 'Passes only if the strategy beats its benchmark on information ratio and excess CAGR.',
    aliases: ['Benchmark relative'],
  },
  embargo: {
    term: 'Embargo',
    short: 'Bars skipped between the tuning and testing windows so information cannot leak across.',
    aliases: ['Embargo bars', 'embargo_bars'],
  },
  hypothesis: {
    term: 'Hypothesis',
    short:
      'Why the strategy should make money and who loses it to you. Written before the first test.',
    aliases: ['Research hypothesis'],
  },
  premortem: {
    term: 'Premortem',
    short: 'How you expect the strategy to fail, written before it does, so you know when to stop.',
    aliases: ['Pre-mortem'],
  },
  label_horizon: {
    term: 'Horizon',
    short: "How many bars ahead the strategy's signal looks; the embargo is at least this long.",
    aliases: ['label_horizon_bars', 'Label horizon'],
  },
  information_coefficient: {
    term: 'Information coefficient (IC)',
    short:
      'How well a signal ranked the moves that followed, from -1 to 1. 0 means no skill, and a steady 0.05 is already useful.',
    aliases: ['IC', 'Mean IC', 'mean_ic', 'IC estimate'],
  },
  violation_ratio: {
    term: 'VaR violation ratio',
    short:
      'Days the loss beat VaR, divided by the days the model expected. Near 1 is right, and outside 0.5 to 1.5 the risk model is off.',
    aliases: ['Violation ratio', 'VaR misses', 'VaR misses against the model', 'Kupiec'],
  },
  alpha_decay: {
    term: 'Alpha decay',
    short:
      "A strategy's live information ratio has fallen well below its backtest, so its edge may be fading.",
    aliases: ['Decay', 'Decayed'],
  },
  concentration: {
    term: 'Concentration',
    short:
      'How much of the book sits in a few names. Effective holdings is how many equal positions it acts like.',
    aliases: ['Effective holdings', 'Largest holding', 'Top 5 weight'],
  },
  twr: {
    term: 'Time-weighted return',
    short:
      'How the investments did, with deposits and withdrawals taken out, so adding money is never profit.',
    aliases: ['TWR', 'Return (time-weighted)'],
  },
  mwr: {
    term: 'Money-weighted return',
    short:
      'The yearly rate your money earned, counting when you added or took it out. Also called XIRR.',
    aliases: ['MWR', 'XIRR', 'Money-weighted, per year'],
  },
};

export interface GlossaryMatch {
  key: GlossaryKey;
  entry: GlossaryEntry;
}

/** "Max drawdown", "max_drawdown" and "MAX-DRAWDOWN" all normalise to "maxdrawdown". */
function normalise(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9%]+/g, '');
}

const INDEX: ReadonlyMap<string, GlossaryKey> = (() => {
  const map = new Map<string, GlossaryKey>();
  for (const key of GLOSSARY_KEYS) {
    const entry = GLOSSARY[key];
    for (const name of [key, entry.term, ...(entry.aliases ?? [])]) {
      const norm = normalise(name);
      if (norm && !map.has(norm)) map.set(norm, key);
    }
  }
  return map;
})();

/** Look a term up by key, display name, visible label or API key. */
export function findGlossary(labelOrKey: string | null | undefined): GlossaryMatch | null {
  if (!labelOrKey) return null;
  const key = INDEX.get(normalise(labelOrKey));
  return key ? { key, entry: GLOSSARY[key] } : null;
}
