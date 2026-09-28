import { GLOSSARY, GLOSSARY_KEYS, findGlossary, glossaryUrl } from './glossary';

/**
 * Every metric label a shared tile, table column or survival-test row shows
 * today. When a page adds a metric, add its label here; the test then fails
 * until the glossary explains it.
 */
const LABELS_SHOWN = [
  // Insights and risk
  'Violation ratio',
  'Effective holdings',
  'Largest holding',
  'Time-weighted return',
  'Money-weighted return',
  'Gross exposure',
  'Net exposure',
  'Beta',
  'Top 5 weight',
  "Strategy's part",
  'Strategy sleeves',
  // Tax, cash flows and alerts
  'Base currency',
  'Lots',
  'FIFO',
  'Specific lots',
  'Cost basis',
  'Wash sale adjustment',
  'Long term',
  'Net deposits',
  'Price alerts',
  'Quiet hours',
  // Signal research
  'Mean IC',
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
    for (const key of GLOSSARY_KEYS) {
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
    for (const key of GLOSSARY_KEYS) {
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

  it("explains the trader's first-hour words (UX-42)", () => {
    for (const word of [
      'Dry run',
      'Auto',
      'Kill switch',
      'Paper trading',
      'Shadow',
      'Live',
      'Signals only',
      'Notify',
      'Stop new buys only',
      'Buys only',
      'Circuit breaker',
      'Trading run',
      'Tick',
      'Fill',
      'Universe',
      'Recovery code',
    ]) {
      expect(findGlossary(word), word).not.toBeNull();
    }
    expect(findGlossary('Dry run')!.key).toBe('dry_run');
    expect(findGlossary('Auto')!.key).toBe('auto');
    expect(findGlossary('Kill switch')!.key).toBe('kill_switch');
    // The system's name is given too, so operators and traders meet in the middle.
    expect(findGlossary('Trading run')!.entry.short).toContain('Also called a tick');
    expect(findGlossary('Paper trading')!.entry.short).toContain('Also called shadow');
  });

  it('links to the in-app glossary, never an outside wiki (UI-13)', () => {
    expect(glossaryUrl()).toBe('/help/glossary');
    expect(glossaryUrl('sharpe')).toBe('/help/glossary#sharpe');
  });

  it('explains TWR, MWR and exposure with a worked example (area 5)', () => {
    for (const key of ['twr', 'mwr', 'gross_exposure', 'net_exposure'] as const) {
      expect(GLOSSARY[key].example, key).toMatch(/\d/);
    }
    expect(findGlossary('Gross exposure')!.key).toBe('gross_exposure');
    expect(findGlossary('Net exposure')!.key).toBe('net_exposure');
    expect(findGlossary('Price alerts')!.entry.short).toContain('close');
  });
});
