import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { listTradeDecisions } from './generated/sdk.gen';

export interface DecisionQuery {
  portfolioId?: string | null;
  ticker?: string | null;
  strategyId?: string | null;
  limit?: number;
}

/**
 * Why did or didn't we trade: per tick and ticker, the step that kept a
 * ticker out of a book or trimmed it (rank, constructor, buffer, stale
 * price, a named risk rule, a halt).
 */
@Injectable({ providedIn: 'root' })
export class DecisionsService {
  list(q: DecisionQuery) {
    return unwrap(
      listTradeDecisions({
        query: {
          portfolio_id: q.portfolioId || undefined,
          ticker: q.ticker || undefined,
          strategy_id: q.strategyId || undefined,
          limit: q.limit ?? 20,
        },
      }),
    );
  }
}
