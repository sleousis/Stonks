import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import { defaultParamValues } from '../../shared/ui/param-form/param-spec';
import { SURVIVAL_TEST_CATALOG } from './lab-test-fixtures';
import { optionCatalog } from './test-options';
import {
  type BacktestForm,
  type LabRunForm,
  backtestErrors,
  buildBacktestRequest,
  buildLabRunRequest,
  defaultBacktestForm,
  defaultLabRunForm,
  defaultWindow,
  benchmarkValue,
  groupStrategies,
  labRunErrors,
  parseTickers,
  suiteTests,
} from './lab-requests';

const TODAY = new Date(2026, 8, 26);

function backtestForm(patch: Partial<BacktestForm> = {}): BacktestForm {
  return {
    ...defaultBacktestForm(TODAY),
    classPath: MOMENTUM.class_path,
    tickers: 'aapl.us, msft.us',
    params: defaultParamValues(MOMENTUM.parameters),
    ...patch,
  };
}

function labForm(patch: Partial<LabRunForm> = {}): LabRunForm {
  return {
    ...defaultLabRunForm(TODAY),
    classPath: MOMENTUM.class_path,
    tickers: 'AAPL.US',
    ...patch,
  };
}

describe('lab requests', () => {
  it('parses tickers from commas, spaces and lines, uppercased and deduplicated', () => {
    expect(parseTickers(' aapl.us,MSFT.US\nspy.us; aapl.us  ')).toEqual([
      'AAPL.US',
      'MSFT.US',
      'SPY.US',
    ]);
    expect(parseTickers('  ')).toEqual([]);
  });

  it('defaults to the last year', () => {
    expect(defaultWindow(TODAY)).toEqual({ start: '2025-09-26', end: '2026-09-26' });
  });

  describe('backtest', () => {
    it('builds the request with params and the configured costs', () => {
      expect(buildBacktestRequest(backtestForm(), MOMENTUM)).toEqual({
        strategy: {
          class_path: MOMENTUM.class_path,
          params: {
            lookback_days: 20,
            threshold: 0.01,
            mode: 'fast',
            long_only: true,
            ticker: 'SPY.US',
          },
        },
        universe: ['AAPL.US', 'MSFT.US'],
        start: '2025-09-26',
        end: '2026-09-26',
        interval: '1d',
        initial_cash: 10_000,
        rebalance_every_bars: 1,
      });
    });

    it('sends a cost preset or flat costs, never both', () => {
      const preset = buildBacktestRequest(backtestForm({ cost: 'realistic' }), MOMENTUM);
      expect(preset.cost_model).toBe('realistic');
      expect(preset.slippage_bps).toBeUndefined();
      expect(preset.fee_per_trade).toBeUndefined();

      const flat = buildBacktestRequest(
        backtestForm({ cost: 'flat', slippageBps: 7.5, feePerTrade: 1 }),
        MOMENTUM,
      );
      expect(flat.cost_model).toBeUndefined();
      expect(flat).toMatchObject({ slippage_bps: 7.5, fee_per_trade: 1 });
    });

    it('validates the window, tickers, params and amounts', () => {
      const errors = backtestErrors(
        backtestForm({
          tickers: ' ',
          start: '2026-02-01',
          end: '2026-01-01',
          initialCash: 0,
          rebalanceEveryBars: 1.5,
          params: { ...defaultParamValues(MOMENTUM.parameters), lookback_days: 1 },
        }),
        MOMENTUM,
      );
      expect(errors).toEqual({
        tickers: 'Enter at least one ticker.',
        end: 'End must be after start.',
        initialCash: 'Enter a positive amount.',
        rebalanceEveryBars: 'Enter a whole number of at least 1.',
        'param.lookback_days': 'At least 5.',
      });
      expect(backtestErrors(backtestForm({ classPath: '' }), null)['strategy']).toBe(
        'Pick a strategy class.',
      );
      expect(backtestErrors(backtestForm(), MOMENTUM)).toEqual({});
    });

    it('sends a chosen benchmark', () => {
      expect(buildBacktestRequest(backtestForm({ benchmark: 'auto' }), MOMENTUM).benchmark).toBe(
        'auto',
      );
      expect(buildBacktestRequest(backtestForm(), MOMENTUM)).not.toHaveProperty('benchmark');
      const errors = backtestErrors(
        backtestForm({ benchmark: 'ticker', benchmarkTicker: 'two words' }),
        MOMENTUM,
      );
      expect(errors['benchmarkTicker']).toBe('One ticker, like QQQ.US.');
    });
  });

  describe('lab run', () => {
    it('sends a preset, not a test list, and no options by default', () => {
      expect(buildLabRunRequest(labForm())).toEqual({
        strategy: { class_path: MOMENTUM.class_path },
        universe: ['AAPL.US'],
        start: '2025-09-26',
        end: '2026-09-26',
        interval: '1d',
        tuner: 'random',
        budget: 20,
        seed: 0,
        objective: 'sharpe',
        train_ratio: 0.7,
        preset: 'quick',
      });
      expect(buildLabRunRequest(labForm({ suite: 'promotion' }))).toMatchObject({
        preset: 'promotion',
      });
      expect(suiteTests({ suite: 'standard', tests: [] })).toContain('walk_forward');
    });

    it('sends a custom suite as survival_tests in canonical order', () => {
      const body = buildLabRunRequest(
        labForm({ suite: 'custom', tests: ['mcpt', 'oos', 'pbo', 'walk_forward'] }),
      );
      expect(body.preset).toBeUndefined();
      expect(body.survival_tests).toEqual(['oos', 'walk_forward', 'pbo', 'mcpt']);
    });

    it('registers only if the tests pass unless told to always register', () => {
      const ifPasses = buildLabRunRequest(
        labForm({ register: true, hypothesis: '  Winners keep winning.  ', premortem: 'Chop.' }),
      );
      expect(ifPasses).toMatchObject({
        register_if_passes: true,
        hypothesis: 'Winners keep winning.',
        premortem: 'Chop.',
      });
      expect(ifPasses.register_strategy).toBeUndefined();

      const always = buildLabRunRequest(
        labForm({ register: true, registerIfPasses: false, hypothesis: 'x' }),
      );
      expect(always.register_strategy).toBe(true);
      expect(always.register_if_passes).toBeUndefined();

      expect(labRunErrors(labForm({ register: true }))['hypothesis']).toBe(
        'Say why it should make money before registering it.',
      );
      expect(labRunErrors(labForm({ register: false }))['hypothesis']).toBeUndefined();
    });

    it('sends the benchmark and embargo only when set', () => {
      expect(buildLabRunRequest(labForm({ benchmark: 'EW', embargoBars: 5 }))).toMatchObject({
        benchmark: 'EW',
        embargo_bars: 5,
      });
      expect(
        buildLabRunRequest(labForm({ benchmark: 'ticker', benchmarkTicker: ' qqq.us ' })).benchmark,
      ).toBe('QQQ.US');
      expect(buildLabRunRequest(labForm({ benchmark: 'none' })).benchmark).toBe('none');
      expect(buildLabRunRequest(labForm({ benchmark: 'default' }))).not.toHaveProperty('benchmark');
      expect(benchmarkValue({ benchmark: 'auto', benchmarkTicker: '' })).toBe('auto');
      expect(labRunErrors(labForm({ benchmark: 'ticker' }))['benchmarkTicker']).toBe(
        'Enter a ticker, e.g. QQQ.US.',
      );
      expect(labRunErrors(labForm({ embargoBars: -1 }))['embargoBars']).toBeDefined();
    });

    it('adds walk-forward and MCPT options only with their tests and only when set', () => {
      const body = buildLabRunRequest(
        labForm({
          tuner: 'grid',
          objective: 'cagr',
          suite: 'custom',
          tests: ['mcpt', 'oos', 'walk_forward'],
          wfSplits: 5,
          wfTestDays: 30,
          wfAnchored: true,
          wfMinWfe: 0.6,
          wfMatrix: true,
          mcptPermutations: 200,
          mcptMaxP: 0.01,
          mcptMetric: 'sharpe',
          mcptRetune: 'auto',
        }),
      );
      expect(body.walk_forward).toEqual({
        n_splits: 5,
        test_days: 30,
        anchored: true,
        min_wfe: 0.6,
        matrix: true,
        metric: 'cagr',
      });
      expect(body.mcpt).toEqual({
        n_permutations: 200,
        max_p_value: 0.01,
        metric: 'sharpe',
        retune: 'auto',
      });
      expect(buildLabRunRequest(labForm({ suite: 'promotion', mcptRetune: 'no' })).mcpt).toEqual({
        retune: false,
      });

      // The preset keeps its own settings when nothing is changed.
      const untouched = buildLabRunRequest(labForm({ suite: 'promotion' }));
      expect(untouched).not.toHaveProperty('walk_forward');
      expect(untouched).not.toHaveProperty('mcpt');
      // Walk-forward settings are dropped when the suite does not run it.
      expect(buildLabRunRequest(labForm({ wfSplits: 5 }))).not.toHaveProperty('walk_forward');
    });

    it('validates tuner and survival options', () => {
      const errors = labRunErrors(
        labForm({
          budget: 0,
          trainRatio: 1,
          suite: 'custom',
          tests: ['walk_forward', 'mcpt'],
          wfSplits: 1,
          wfTestDays: 2.5,
          wfMinWfe: -1,
          mcptPermutations: 5000,
          mcptMaxP: 0,
        }),
      );
      expect(Object.keys(errors).sort()).toEqual(
        [
          'budget',
          'mcptMaxP',
          'mcptPermutations',
          'trainRatio',
          'wfMinWfe',
          'wfSplits',
          'wfTestDays',
        ].sort(),
      );
      expect(labRunErrors(labForm({ suite: 'custom', tests: [] }))['tests']).toBe(
        'Pick at least one survival test.',
      );
      expect(labRunErrors(labForm())).toEqual({});
    });

    it('builds test_options per test from the API catalog, with field errors', () => {
      const catalog = optionCatalog(SURVIVAL_TEST_CATALOG);
      const form = labForm({
        suite: 'standard',
        testOptions: {
          oos: { min_psr: '0.9', mode: '' },
          cost_stress: { multipliers: '1, 2' },
          deflated_sharpe: { include_prior_runs: 'false' },
          // Not in the standard suite: ignored.
          mcpt: { seed: '3' },
        },
      });
      expect(labRunErrors(form, catalog)).toEqual({});
      expect(buildLabRunRequest(form, catalog).test_options).toEqual({
        oos: { min_psr: 0.9 },
        deflated_sharpe: { include_prior_runs: false },
        cost_stress: { multipliers: [1, 2] },
      });

      const bad = labForm({
        suite: 'standard',
        testOptions: { deflated_sharpe: { min_dsr: '2' }, oos: { min_trades: 'lots' } },
      });
      expect(labRunErrors(bad, catalog)).toEqual({
        'opt.deflated_sharpe.min_dsr': 'Must be at least 0.8 and at most 0.99.',
        'opt.oos.min_trades': 'Enter a number.',
      });
      expect(buildLabRunRequest(labForm(), catalog).test_options).toBeUndefined();
      // Without the catalog nothing can be checked or sent.
      expect(buildLabRunRequest(form).test_options).toBeUndefined();
    });
  });

  it('groups and filters the catalog', () => {
    expect(groupStrategies(CATALOG).map((g) => [g.label, g.classes.map((c) => c.name)])).toEqual([
      ['Examples', ['buy_and_hold', 'momentum']],
      ['Strategies', ['macro_regime']],
    ]);
    expect(groupStrategies(CATALOG, 'trailing').flatMap((g) => g.classes)).toEqual([MOMENTUM]);
  });
});
