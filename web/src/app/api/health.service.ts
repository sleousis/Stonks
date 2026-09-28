import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getHealth, getHealthReport, getPriceCheck, runHealthChecks } from './generated/sdk.gen';
import type { GetHealthReportData } from './models';

/** Liveness (version) and the operational health report (freshness, ticks, …). */
@Injectable({ providedIn: 'root' })
export class HealthService {
  ping() {
    return unwrap(getHealth());
  }

  report(query?: GetHealthReportData['query']) {
    return unwrap(getHealthReport({ query }));
  }

  /** The newest second-source price check, or null before the first (roadmap 23.6). */
  priceCheck() {
    return unwrap(getPriceCheck());
  }

  /**
   * Run every check now, like the scheduled health job: it opens the
   * operational halt on stale data or a stuck run and clears it once the
   * checks pass. Admins only.
   */
  runChecks(tickers?: string[]) {
    return unwrap(runHealthChecks({ body: { tickers: tickers?.length ? tickers : null } }));
  }
}
