import type { GoLiveCheckView, GoLiveReport } from '../api/models';
import { VERDICT_WORDS, failReason, strategyVerdict } from './strategy-verdict';

function check(
  name: GoLiveCheckView['name'],
  passed: boolean,
  value: number | null = null,
  limit: number | null = null,
): GoLiveCheckView {
  return { name, passed, value, limit, detail: `${name} detail` };
}

function report(checks: GoLiveCheckView[]): GoLiveReport {
  return {
    strategy_id: 'mom_0a1b2c3d',
    status: 'shadow',
    source: 'shadow',
    passed: checks.every((c) => c.passed),
    policy: {},
    checks,
  };
}

const trial = { total_return: 0.03, max_drawdown: -0.04, days: 40 };

describe('strategyVerdict (F33)', () => {
  it('uses the three agreed words', () => {
    expect(Object.values(VERDICT_WORDS)).toEqual([
      'Worth following',
      'Promising, needs more data',
      'Not good enough yet',
    ]);
  });

  it('is worth following when every check passed and it made money on trial', () => {
    const v = strategyVerdict({
      status: 'shadow',
      golive: report([check('status', true), check('min_days', true, 70, 63)]),
      trial,
    });
    expect(v.level).toBe('worth');
    expect(v.tone).toBe('positive');
    expect(v.reasons[0]).toBe('It passed all 2 checks of the go-live check.');
    expect(v.reasons[1]).toMatch(/^On trial it made \+3\.00% over 40 days/);
  });

  it('needs more data when only time-bound checks fail', () => {
    const v = strategyVerdict({
      status: 'shadow',
      golive: report([check('min_days', false, 12, 63), check('min_trades', false, 2, 10)]),
      trial: { ...trial, days: 12 },
    });
    expect(v.level).toBe('promising');
    expect(v.label).toBe('Promising, needs more data');
    expect(v.reasons).toContain('It has 12 of the 63 trial days it needs.');
    expect(v.reasons).toContain('It made 2 of the 10 trial trades it needs.');
  });

  it('is not good enough when a check shows a bad sign', () => {
    const v = strategyVerdict({
      status: 'shadow',
      golive: report([check('max_drawdown', false, -0.3, 0.2), check('min_days', false, 5, 63)]),
      trial,
    });
    expect(v.level).toBe('not_yet');
    expect(v.tone).toBe('negative');
    expect(v.reasons[0]).toContain('worst drop on trial');
  });

  it('holds back a passed check while the trial is losing money', () => {
    const v = strategyVerdict({
      status: 'active',
      golive: report([check('status', true)]),
      trial: { ...trial, total_return: -0.02 },
    });
    expect(v.level).toBe('promising');
    expect(v.reasons[0]).toMatch(/lost 2\.00%.*Give it more time/);
  });

  it('falls back to the robustness tests without a go-live report', () => {
    expect(
      strategyVerdict({ status: 'retired', golive: null, tests: { passed: 3, total: 4 } }).level,
    ).toBe('not_yet');
    expect(
      strategyVerdict({ status: 'shadow', golive: null, golivePassed: true, trial }).level,
    ).toBe('worth');
    const none = strategyVerdict({ status: 'shadow', golive: null });
    expect(none.level).toBe('promising');
    expect(none.reasons).toContain('It has no trial record yet.');
  });

  it('never names a system word in a reason', () => {
    const names: GoLiveCheckView['name'][] = [
      'status',
      'min_days',
      'max_drawdown',
      'max_drift',
      'min_trades',
      'survival',
      'within_mc_band',
      'quit_rule',
      'promotion_preset',
      'nonzero_costs',
      'hypothesis_recorded',
      'backtest_min_trades',
    ];
    for (const n of names) {
      expect(failReason(check(n, false, 1, 2))).not.toMatch(/shadow|promot|survival|live/i);
    }
  });
});
