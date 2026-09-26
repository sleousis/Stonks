import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import { listPortfolios } from './generated/sdk.gen';
import type { PortfolioSummaryView } from './models';

/**
 * One of the caller's portfolios (`GET /api/portfolios`): paper or live,
 * and which one reads use when no `portfolio_id` is sent.
 */
export type PortfolioRef = PortfolioSummaryView;

/** The caller's portfolios. A trader who only follows signals has none. */
@Injectable({ providedIn: 'root' })
export class PortfoliosService {
  /** Silent: pages show their own empty or error state. */
  list(): Promise<PortfolioRef[]> {
    return allItems((query) => unwrap(listPortfolios({ query, headers: SILENT_HEADERS })));
  }
}
