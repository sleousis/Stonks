import { GLOSSARY, METRIC_KEYS, WIKI_GLOSSARY_URL, findGlossary } from './glossary';

/**
 * Every metric label a shared tile, table column or survival-test row shows
 * today. When a page adds a metric, add its label here; the test then fails
 * until the glossary explains it.
 */
const LABELS_SHOWN = [
  // Backtest and lab result tiles
  'Total return',
  'CAGR',
  'Sharpe',
  'Max drawdown',
  'Profit factor',
  'Trades',
  'Best score',
  // Table columns
  'Drawdown',
  'Return',
  'Equity',
  // Survival tests (ids and labels)
  'oos',
  'Out of sample',
  'period_stability',
  'Period stability',
  'perturbation',
  'drift',
  'runs_test',
  'Runs test',
  'walk_forward',
  'Walk-forward',
  'permutation',
  'Monte Carlo permutation',
  // Survival metrics (API keys)
  'is_score',
  'oos_score',
  'p_value',
  'cagr_oos',
  'max_drawdown',
  'final_return',
  // Phase 9 results: risk, trades, benchmark and trial counts
  'Sortino',
  'Calmar',
  'Ulcer index',
  'VaR 95%',
  'ES 95%',
  'Longest drawdown',
  'Win rate',
  'Expectancy',
  'Payoff ratio',
  'Avg holding time',
  'Turnover',
  'Cost drag',
  'Excess CAGR',
  'Alpha',
  'Beta',
  'Information ratio',
  'Up capture',
  'Down capture',
  'Trials this run',
  'Trials of this class',
  // Phase 9 survival tests and their key figures
  'deflated_sharpe',
  'pbo',
  'mc_trades',
  'cost_stress',
  'plateau',
  'cross_instrument',
  'benchmark_relative',
  'mcpt',
  'dsr',
  'psr0',
  'wfe',
  'p95_max_dd',
  'risk_of_ruin',
  'break_even_multiple',
  'n_trials',
  // Lab form and research card
  'Embargo bars',
  'Benchmark',
  'Hypothesis',
  'Premortem',
  'label_horizon',
];

/** The metrics the roadmap names for in-app help (13.11). */
const REQUIRED = [
  'Sharpe',
  'Deflated Sharpe',
  'Probabilistic Sharpe',
  'Drawdown',
  'Max drawdown',
  'CAGR',
  'Sortino',
  'Calmar',
  'Profit factor',
  'Expectancy',
  'PBO',
  'MinTRL',
  'Alpha',
  'Beta',
  'Information ratio',
  'Tracking error',
  'VaR',
  'ES',
  'CVaR',
  'Volatility',
  'Win rate',
  'Turnover',
];

describe('glossary', () => {
  it('has a plain one-line entry for every metric key', () => {
    for (const key of METRIC_KEYS) {
      const entry = GLOSSARY[key];
      expect(entry, key).toBeDefined();
      expect(entry.term.length, key).toBeGreaterThan(0);
      expect(entry.short.length, key).toBeGreaterThan(10);
      // One or two short sentences: fits a phone-width tip.
      expect(entry.short.length, key).toBeLessThanOrEqual(140);
      expect(entry.short.endsWith('.'), key).toBe(true);
    }
  });

  it('explains every metric label the console shows', () => {
    const missing = LABELS_SHOWN.filter((label) => !findGlossary(label));
    expect(missing).toEqual([]);
  });

  it('explains every metric the roadmap asks for', () => {
    const missing = REQUIRED.filter((label) => !findGlossary(label));
    expect(missing).toEqual([]);
  });

  it('never maps one name to two entries', () => {
    const seen = new Map<string, string>();
    for (const key of METRIC_KEYS) {
      const e = GLOSSARY[key];
      for (const name of [key, e.term, ...(e.aliases ?? [])]) {
        const norm = name.toLowerCase().replace(/[^a-z0-9%]+/g, '');
        const owner = seen.get(norm);
        expect(owner === undefined || owner === key, `${name}: ${owner} and ${key}`).toBe(true);
        seen.set(norm, key);
      }
    }
  });

  it('matches labels loosely and ignores non-metrics', () => {
    expect(findGlossary('Max drawdown')?.key).toBe('max_drawdown');
    expect(findGlossary('MAX-DRAWDOWN')?.key).toBe('max_drawdown');
    expect(findGlossary('sharpe')?.key).toBe('sharpe');
    expect(findGlossary('Cash')).toBeNull();
    expect(findGlossary('Ticker')).toBeNull();
    expect(findGlossary('')).toBeNull();
    expect(findGlossary(null)).toBeNull();
  });

  it('links to the wiki glossary', () => {
    expect(WIKI_GLOSSARY_URL).toBe('https://github.com/sleousis/Stonks/wiki/Glossary');
  });
});
