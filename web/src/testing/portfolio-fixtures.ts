import type { PortfolioRef } from '../app/api/portfolios.service';

/** A portfolio as `GET /api/portfolios` lists it; only `id` and `name` are required here. */
export function book(
  over: Partial<PortfolioRef> & Pick<PortfolioRef, 'id' | 'name'>,
): PortfolioRef {
  return {
    kind: 'simulated',
    status: 'active',
    base_currency: 'USD',
    initial_cash: 10_000,
    broker_connection_id: null,
    trading: 'paper',
    created_at: '2026-09-01T00:00:00Z',
    is_default: false,
    ...over,
  };
}
