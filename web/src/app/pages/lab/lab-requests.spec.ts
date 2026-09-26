import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import { defaultParamValues } from '../../shared/ui/param-form/param-spec';
import {
  type BacktestForm,
  type LabRunForm,
  backtestErrors,
  buildBacktestRequest,
  buildLabRunRequest,
  defaultBacktestForm,
  defaultLabRunForm,
  defaultWindow,
  groupStrategies,
  labRunErrors,
  parseTickers,
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
  });

  describe('lab run', () => {
    it('builds the default request without walk-forward or MCPT options', () => {
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
        survival_tests: ['oos', 'period_stability'],
        register_strategy: false,
      });
    });

    it('adds walk-forward and MCPT options only with their tests, in canonical order', () => {
      const body = buildLabRunRequest(
        labForm({
          tuner: 'grid',
          objective: 'cagr',
          tests: ['permutation', 'oos', 'walk_forward'],
          wfSplits: 5,
          wfTestDays: 30,
          wfAnchored: true,
          mcptPermutations: 200,
          mcptMaxP: 0.01,
          mcptMetric: 'sharpe',
          mcptRetune: true,
          register: true,
        }),
      );
      expect(body.survival_tests).toEqual(['oos', 'walk_forward', 'permutation']);
      expect(body.walk_forward).toEqual({
        n_splits: 5,
        test_days: 30,
        anchored: true,
        metric: 'cagr',
      });
      expect(body.mcpt).toEqual({
        n_permutations: 200,
        max_p_value: 0.01,
        metric: 'sharpe',
        retune: true,
      });
      expect(body).toMatchObject({ tuner: 'grid', register_strategy: true });

      const evenFolds = buildLabRunRequest(labForm({ tests: ['walk_forward'] }));
      expect(evenFolds.walk_forward).not.toHaveProperty('test_days');
    });

    it('validates tuner and survival options', () => {
      const errors = labRunErrors(
        labForm({
          budget: 0,
          trainRatio: 1,
          tests: ['walk_forward', 'permutation'],
          wfSplits: 0,
          wfTestDays: 2.5,
          mcptPermutations: 5000,
          mcptMaxP: 0,
        }),
      );
      expect(Object.keys(errors).sort()).toEqual(
        ['budget', 'mcptMaxP', 'mcptPermutations', 'trainRatio', 'wfSplits', 'wfTestDays'].sort(),
      );
      expect(labRunErrors(labForm({ tests: [] }))['tests']).toBe(
        'Pick at least one survival test.',
      );
      expect(labRunErrors(labForm())).toEqual({});
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
