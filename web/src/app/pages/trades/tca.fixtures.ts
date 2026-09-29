// Test data for the trade cost specs.
import type { JournalEntryView, TcaGroupView, TcaSummaryView } from '../../api/models';

export function group(overrides: Partial<TcaGroupView> = {}): TcaGroupView {
  return {
    key: 'all',
    orders: 4,
    filled_orders: 3,
    filled_notional: 10_000,
    is_bps: 12.34,
    is_cost: 12.34,
    delay_bps: 5,
    impact_bps: 6,
    fee_bps: 1.34,
    expected_bps: 8,
    model_gap_bps: 4.34,
    opportunity_bps: 20,
    opportunity_cost: 2.5,
    convention_bps: 1,
    ...overrides,
  };
}

export function summary(overrides: Partial<TcaSummaryView> = {}): TcaSummaryView {
  return {
    by: 'all',
    groups: [group()],
    portfolio_id: 'pf_default',
    since: null,
    until: null,
    ...overrides,
  };
}

export const CLIENT_ID = '2026-09-25:momentum-v3:AAPL.US:buy';

export function entry(overrides: Partial<JournalEntryView> = {}): JournalEntryView {
  return {
    client_id: CLIENT_ID,
    context: { trigger: 'signal', score: 0.82, rank: 1, target_weight: 0.25 },
    created_at: '2026-09-25T21:00:01Z',
    decided_at: '2026-09-25T21:00:00Z',
    decision_price: 200,
    next_session_move_bps: 15,
    notes: [],
    portfolio_id: 'pf_default',
    quantity: 10,
    shortfall: {
      side: 'buy',
      ordered_quantity: 10,
      filled_quantity: 10,
      decision_price: 200,
      arrival_price: 200.1,
      fill_price: 200.3,
      benchmark_price: 200.05,
      post_close_price: 201,
      expected_bps: 8,
      delay_bps: 5,
      impact_bps: 10,
      fee_bps: 2,
      is_bps: 17,
      is_cost: 3.4,
      opportunity_bps: null,
      opportunity_cost: 0,
      convention_bps: 2.5,
      total_bps: 17,
    },
    side: 'buy',
    status: 'filled',
    status_reason: null,
    strategy_id: 'momentum-v3',
    strategy_name: null,
    ticker: 'AAPL.US',
    trigger: 'signal',
    ...overrides,
  };
}
