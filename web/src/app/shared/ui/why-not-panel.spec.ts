import type { TradeDecisionView } from '../../api/models';
import { OUTCOME_LABELS, decisionDetail, groupDecisions } from './why-not-panel';

function row(
  step: string,
  detail: Record<string, unknown>,
  extra: Partial<TradeDecisionView> = {},
): TradeDecisionView {
  return {
    tick_id: 't1',
    portfolio_id: 'pf',
    as_of: '2026-03-20',
    ticker: 'AAA.US',
    step,
    outcome: 'kept_out',
    strategy_id: null,
    strategies: [],
    score: null,
    detail,
    summary: 'AAA.US: kept out',
    step_text: '',
    ...extra,
  };
}

describe('why not panel', () => {
  it('names each outcome in plain words', () => {
    expect(OUTCOME_LABELS['kept_out']).toBe('Kept out');
    expect(OUTCOME_LABELS['trimmed']).toBe('Trimmed');
  });

  it('shows the quantities a risk rule changed', () => {
    const text = decisionDetail(
      row('risk_rule', { rule: 'sector_cap', original_quantity: 10, adjusted_quantity: 4 }),
    );
    expect(text).toBe('10 to 4 shares');
  });

  it('shows the quantities the lot rule rounded', () => {
    const text = decisionDetail(row('lots', { original_quantity: 10, adjusted_quantity: 8 }));
    expect(text).toBe('10 to 8 shares');
  });

  it('shows the target and current weight inside the buffer', () => {
    expect(decisionDetail(row('buffer', { target_weight: 0.5, current_weight: 0.48 }))).toBe(
      'target 50%, now 48%',
    );
  });

  it('names the pick that won the book', () => {
    expect(decisionDetail(row('rank', { winner: 'mom' }))).toBe('mom won the book');
    expect(decisionDetail(row('held', {}))).toBe('');
  });
});

describe('grouping the decisions', () => {
  const items = [
    row('traded', {}, { tick_id: 't2', as_of: '2026-03-21', ticker: 'A', outcome: 'traded' }),
    row('held', {}, { tick_id: 't2', as_of: '2026-03-21', ticker: 'B', outcome: 'held' }),
    row('rank', {}, { tick_id: 't1', as_of: '2026-03-20', ticker: 'C' }),
  ];

  it('shows only the latest run and hides plain holds by default', () => {
    const groups = groupDecisions(items, false);
    expect(groups.map((g) => g.day)).toEqual(['2026-03-21']);
    expect(groups[0].rows.map((r) => r.ticker)).toEqual(['A']);
  });

  it('shows every row, grouped by day, when asked', () => {
    const groups = groupDecisions(items, true);
    expect(groups.map((g) => [g.day, g.rows.length])).toEqual([
      ['2026-03-21', 2],
      ['2026-03-20', 1],
    ]);
  });
});
