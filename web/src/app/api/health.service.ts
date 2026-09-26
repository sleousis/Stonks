import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getHealth, getHealthReport } from './generated/sdk.gen';
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
}
