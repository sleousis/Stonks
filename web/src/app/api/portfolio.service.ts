import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import {
  getPnl,
  getPortfolio,
  getPortfolioTotals,
  listPortfolioSnapshots,
} from './generated/sdk.gen';
import type { GetPnlData, ListPortfolioSnapshotsData } from './models';

/**
 * Portfolio value, positions at latest prices, snapshots and the P&L series,
 * for the portfolio picked in the session strip (`portfolio_id` is added
 * from PortfolioContextService unless the caller passes one).
 */
@Injectable({ providedIn: 'root' })
export class PortfolioService {
  private readonly ctx = inject(PortfolioContextService);

  get() {
    return unwrap(getPortfolio({ query: this.ctx.query() }));
  }

  snapshots(query?: ListPortfolioSnapshotsData['query']) {
    return unwrap(listPortfolioSnapshots({ query: { ...this.ctx.query(), ...query } }));
  }

  /** Daily P&L rows (value, returns, drawdown) of the real simulated portfolio. */
  pnl(query?: GetPnlData['query']) {
    return unwrap(getPnl({ query: { ...this.ctx.query(), ...query } }));
  }

  /** Admins: cash and value summed across every trader. No holdings, no names. */
  totals() {
    return unwrap(getPortfolioTotals());
  }
}
