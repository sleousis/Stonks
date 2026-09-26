import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getPnl, getPortfolio, listPortfolioSnapshots } from './generated/sdk.gen';
import type { GetPnlData, ListPortfolioSnapshotsData } from './models';

/** Portfolio value, positions at latest prices, snapshots and the P&L series. */
@Injectable({ providedIn: 'root' })
export class PortfolioService {
  get() {
    return unwrap(getPortfolio());
  }

  snapshots(query?: ListPortfolioSnapshotsData['query']) {
    return unwrap(listPortfolioSnapshots({ query }));
  }

  /** Daily P&L rows (value, returns, drawdown) of the real simulated portfolio. */
  pnl(query?: GetPnlData['query']) {
    return unwrap(getPnl({ query }));
  }
}
