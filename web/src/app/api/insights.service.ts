import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import {
  getBehaviourReport,
  getInsights,
  getInsightsTotals,
  getStrategyAgreement,
} from './generated/sdk.gen';
import type { GetInsightsData } from './models';

/**
 * Portfolio insights for the picked portfolio (a synced broker account too):
 * allocation, exposure, P&L over periods and risk, and which active
 * strategies agree with each holding. Admins also get totals across every
 * book, never holdings.
 */
@Injectable({ providedIn: 'root' })
export class InsightsService {
  private readonly ctx = inject(PortfolioContextService);

  get(query?: GetInsightsData['query']) {
    return unwrap(getInsights({ query: { ...this.ctx.query(), ...query } }));
  }

  agreement() {
    return unwrap(getStrategyAgreement({ query: this.ctx.query() }));
  }

  /** How you trade by hand: manual and synced trades as round trips. */
  behaviour(since?: string) {
    return unwrap(getBehaviourReport({ query: { ...this.ctx.query(), since: since ?? null } }));
  }

  /** Admins: asset-class allocation and exposure summed over every book. */
  totals() {
    return unwrap(getInsightsTotals());
  }
}
