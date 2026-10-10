import { testLabel, testReason } from './survival-tests';
import { BACKTEST_RESULT_FULL } from '../../../testing/lab-fixtures';
import { benchmarkHeadline } from './lab-run-result';
import { signalIcEmptyText } from './signal-ic-result';

describe('testReason', () => {
  const r = (test_id: string, passed: boolean, notes: string) => ({ test_id, passed, notes });

  it('turns machine notes into one plain sentence', () => {
    expect(testReason(r('oos', false, 'insufficient trades: 5 < 20'))).toBe(
      'Only 5 trades on the unseen data, it needs 20.',
    );
    expect(testReason(r('oos', false, 'max drawdown -0.420 below -0.3'))).toBe(
      'Its worst drop on the unseen data was -42.0%, the limit is -30.0%.',
    );
    expect(
      testReason(
        r('walk_forward', false, 'metric=sharpe; rolling; embargo=0d; failed: wfe 0.10 < 0.5'),
      ),
    ).toBe('It kept too little of its past return in the retests.');
  });

  it('never shows raw notes', () => {
    expect(testReason(r('period_stability', true, 'window=val'))).toBe(
      'It did about as well in each part of the window.',
    );
    expect(testReason(r('perturbation', false, 'seed=0; window=val'))).not.toContain('=');
    expect(testReason(r('odd_test', true, 'k=v'))).toBe('It passed this test.');
  });

  it('says how many tickers the signal check needs', () => {
    expect(
      testReason(
        r(
          'signal_ic',
          true,
          'n/a: universe has 8 tickers; IC analysis needs at least 10; window x',
        ),
      ),
    ).toBe('It needs at least 10 tickers, this run had 8.');
  });
});

describe('testLabel', () => {
  it('uses plain names, no acronyms', () => {
    expect(testLabel('deflated_sharpe')).toBe('Luck from many tries');
    expect(testLabel('pbo')).toBe('Picked the luckiest setting');
    expect(testLabel('mcpt')).toBe('Shuffled prices');
    expect(testLabel('cpcv')).toBe('Many data splits');
    expect(testLabel('walk_forward')).toBe('Rolling retest');
  });
});

describe('benchmarkHeadline', () => {
  it('puts the held-out return and worst drop next to the benchmark', () => {
    const figures = benchmarkHeadline({
      benchmark: {
        ...BACKTEST_RESULT_FULL.benchmark!,
        benchmark_cagr: 0.1,
        benchmark_max_dd: -0.12,
      },
      survival_reports: [
        {
          test_id: 'oos',
          passed: true,
          metrics: { cagr_oos: 0.25, max_drawdown_oos: -0.171 },
          notes: '',
        },
      ],
    });
    expect(figures.map((f) => [f.label, f.value])).toEqual([
      ['Yearly return vs benchmark', '25.00% vs 10.00%'],
      ['Worst drop vs benchmark', '-17.10% vs -12.00%'],
    ]);
  });
});

describe('signalIcEmptyText', () => {
  it('says plainly how many tickers the check needs', () => {
    expect(
      signalIcEmptyText({
        n_tickers: 8,
        note: 'universe has 8 tickers; IC analysis needs at least 10',
      }),
    ).toBe('Not enough tickers to rank. The check needs at least 10, this one had 8.');
    expect(signalIcEmptyText({ n_tickers: 12 })).toContain('Not enough data');
  });
});
