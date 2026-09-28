import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getStreamStatus, listIntradaySnapshots } from './generated/sdk.gen';

/** The live intraday engine: stream health, latency and the silent-engine alarm. */
@Injectable({ providedIn: 'root' })
export class StreamService {
  status() {
    return unwrap(getStreamStatus());
  }

  /**
   * The latest intraday P&L row of one portfolio (the whole book, its
   * latest day). No id: the caller's default portfolio.
   */
  async latestPnl(portfolioId?: string) {
    const page = await unwrap(
      listIntradaySnapshots({
        query: { limit: 1, ...(portfolioId ? { portfolio_id: portfolioId } : {}) },
      }),
    );
    return page.items[0] ?? null;
  }
}
