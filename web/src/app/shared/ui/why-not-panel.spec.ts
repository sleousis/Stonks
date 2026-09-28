import type { TradeDecisionView } from '../../api/generated/types.gen';
import { OUTCOME_LABELS, decisionDetail } from './why-not-panel';

function row(step: string, detail: Record<string, unknown>): TradeDecisionView {
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

  it('shows the target and current weight inside the buffer', () => {
    expect(decisionDetail(row('buffer', { target_weight: 0.5, current_weight: 0.48 }))).toBe(
      'target 0.5, now 0.48',
    );
  });

  it('names the pick that won the book', () => {
    expect(decisionDetail(row('rank', { winner: 'mom' }))).toBe('mom won the book');
    expect(decisionDetail(row('held', {}))).toBe('');
  });
});
