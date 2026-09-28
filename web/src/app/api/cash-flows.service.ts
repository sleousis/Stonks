import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import { listCashFlows, recordCashFlow } from './generated/sdk.gen';
import type { CashFlowCreate } from './generated/types.gen';

/**
 * Deposits and withdrawals of one of your portfolios. Returns are time and
 * money weighted from them, so a deposit never shows as profit. Recording
 * one moves a simulated book's cash.
 */
@Injectable({ providedIn: 'root' })
export class CashFlowsService {
  list(portfolioId: string) {
    return allItems((query) =>
      unwrap(listCashFlows({ path: { portfolio_id: portfolioId }, query })),
    );
  }

  record(portfolioId: string, body: CashFlowCreate) {
    return unwrap(recordCashFlow({ path: { portfolio_id: portfolioId }, body }));
  }
}
