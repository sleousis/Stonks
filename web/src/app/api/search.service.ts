import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import { listInstruments, listJobs, listStrategies } from './generated/sdk.gen';

/**
 * Background reads for the command palette. They run as the trader types,
 * so failures are silent (no error toast): the palette shows its own
 * "could not search" line instead.
 */
@Injectable({ providedIn: 'root' })
export class SearchService {
  /** Registered strategies (every status), for client-side matching. */
  strategies(limit = 200) {
    return unwrap(listStrategies({ query: { limit }, headers: SILENT_HEADERS }));
  }

  /** Instruments whose id or name contains `q`. */
  instruments(q: string, limit = 8) {
    return unwrap(listInstruments({ query: { q, limit }, headers: SILENT_HEADERS }));
  }

  /** Most recent background jobs (ticks, backtests, lab runs, ingests). */
  recentJobs(limit = 8) {
    return unwrap(listJobs({ query: { limit }, headers: SILENT_HEADERS }));
  }
}
