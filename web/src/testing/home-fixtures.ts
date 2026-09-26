import type { SubscriptionView } from '../app/api/subscriptions.service';

export function sub(overrides: Partial<SubscriptionView> = {}): SubscriptionView {
  return {
    id: 'sub_1',
    strategy_id: 'momentum-v3',
    strategy_status: 'active',
    portfolio_id: 'pf_1',
    mode: 'paper',
    enabled: true,
    paper_days_completed: 20,
    paper_days_required: 20,
    auto_blockers: [],
    paused_reason: null,
    ...overrides,
  };
}
