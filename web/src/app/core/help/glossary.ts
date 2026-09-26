// Plain-English, one-line explanations of every figure the console shows.
// The single source for in-app help: <app-help-tip>, stat tiles and table
// headers look terms up here by key or by their visible label. Longer
// definitions live on the wiki Glossary page, which every tip links to.
//
// Add a metric: add a key to METRIC_KEYS, an entry below (the compiler
// insists), and any labels or API keys it appears under as `aliases`.

export const WIKI_GLOSSARY_URL = 'https://github.com/sleousis/Stonks/wiki/Glossary';

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
] as const;

export type MetricKey = (typeof METRIC_KEYS)[number];

export interface GlossaryEntry {
  /** Display name, as a trader would say it. */
  term: string;
  /** One plain-English sentence (two at most). */
  short: string;
  /** Other labels or API keys this term appears under. Matched loosely. */
  aliases?: readonly string[];
}

export const GLOSSARY: Record<MetricKey, GlossaryEntry> = {
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
    aliases: ['Probabilistic Sharpe', 'PSR', 'psr'],
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
    aliases: ['ES', 'CVaR', 'Conditional VaR', 'es_95', 'cvar'],
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
    aliases: ['Trade count', 'n_trades'],
  },
  turnover: {
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
    aliases: ['Jensen alpha'],
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
};

export interface GlossaryMatch {
  key: MetricKey;
  entry: GlossaryEntry;
}

/** "Max drawdown", "max_drawdown" and "MAX-DRAWDOWN" all normalise to "maxdrawdown". */
function normalise(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9%]+/g, '');
}

const INDEX: ReadonlyMap<string, MetricKey> = (() => {
  const map = new Map<string, MetricKey>();
  for (const key of METRIC_KEYS) {
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
