import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getShadowPnl, listShadowDecisions, listShadowPnl } from './generated/sdk.gen';
import type { GetShadowPnlData, ListShadowDecisionsData, ListShadowPnlData } from './models';

/** Shadow strategies: their paper decisions and P&L next to the active ones. */
@Injectable({ providedIn: 'root' })
export class ShadowService {
  decisions(query?: ListShadowDecisionsData['query']) {
    return unwrap(listShadowDecisions({ query }));
  }

  pnlSummaries(query?: ListShadowPnlData['query']) {
    return unwrap(listShadowPnl({ query }));
  }

  pnl(strategyId: string, query?: GetShadowPnlData['query']) {
    return unwrap(getShadowPnl({ path: { strategy_id: strategyId }, query }));
  }
}
