import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { listAlerts } from './generated/sdk.gen';
import type { ListAlertsData } from './models';

/** System alerts the server raised (data gaps, failed runs, broker trouble), newest first. */
@Injectable({ providedIn: 'root' })
export class AlertsService {
  list(query?: ListAlertsData['query']) {
    return unwrap(listAlerts({ query }));
  }
}
