import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import {
  createPriceAlert,
  deletePriceAlert,
  getPriceAlert,
  listPriceAlertEvents,
  listPriceAlerts,
  updatePriceAlert,
} from './generated/sdk.gen';
import type { PriceAlertCreate, PriceAlertUpdate } from './generated/types.gen';

/** Your price alerts on tickers and watchlists, and when they fired. */
@Injectable({ providedIn: 'root' })
export class PriceAlertsService {
  list() {
    return allItems((query) => unwrap(listPriceAlerts({ query })));
  }

  get(id: string) {
    return unwrap(getPriceAlert({ path: { alert_id: id } }));
  }

  create(body: PriceAlertCreate) {
    return unwrap(createPriceAlert({ body }));
  }

  update(id: string, body: PriceAlertUpdate) {
    return unwrap(updatePriceAlert({ path: { alert_id: id }, body }));
  }

  delete(id: string) {
    return unwrap(deletePriceAlert({ path: { alert_id: id } }));
  }

  /** One page of firings, newest first. */
  events(query: { limit?: number; offset?: number; rule_id?: string | null } = {}) {
    return unwrap(listPriceAlertEvents({ query }));
  }
}
