import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import {
  createUniverse,
  deleteUniverse,
  ensureUniverseData,
  getUniverse,
  getUniverseEnsureResult,
  getUniverseHistory,
  getUniverseMembers,
  getUniverseRefreshResult,
  importIndexHistory,
  listUniverseExchanges,
  listUniverses,
  refreshUniverse,
  updateUniverse,
} from './generated/sdk.gen';
import type {
  EnsureDataRequest,
  IndexHistoryImport,
  UniverseCreate,
  UniverseUpdate,
} from './models';

/** One page of membership history, narrowed to tickers containing `ticker`. */
export interface HistoryQuery {
  ticker?: string | null;
  limit?: number;
  offset?: number;
}

/** Stored universes, their point-in-time members, and refresh / ensure-data jobs. */
@Injectable({ providedIn: 'root' })
export class UniversesService {
  list() {
    return allItems((query) => unwrap(listUniverses({ query })));
  }

  get(id: string) {
    return unwrap(getUniverse({ path: { universe_id: id } }));
  }

  create(body: UniverseCreate) {
    return unwrap(createUniverse({ body }));
  }

  /** Replace the definition. The members stay until the next refresh. */
  update(id: string, body: UniverseUpdate) {
    return unwrap(updateUniverse({ path: { universe_id: id }, body }));
  }

  /** Membership spans, latest change first. */
  history(id: string, q: HistoryQuery = {}) {
    return unwrap(
      getUniverseHistory({
        path: { universe_id: id },
        query: { ticker: q.ticker || null, limit: q.limit, offset: q.offset },
      }),
    );
  }

  /** Exchanges our instruments name, for the exchange picker. */
  exchanges() {
    return unwrap(listUniverseExchanges());
  }

  delete(id: string) {
    return unwrap(deleteUniverse({ path: { universe_id: id } }));
  }

  /** Members on `asOf` (today when omitted). */
  members(id: string, asOf?: string | null) {
    return unwrap(
      getUniverseMembers({ path: { universe_id: id }, query: { as_of: asOf || null } }),
    );
  }

  importIndexHistory(body: IndexHistoryImport) {
    return unwrap(importIndexHistory({ body }));
  }

  /** Starts a background job; follow it, then read `refreshResult`. */
  refresh(id: string) {
    return unwrap(refreshUniverse({ path: { universe_id: id } }));
  }

  refreshResult(jobId: string) {
    return unwrap(getUniverseRefreshResult({ path: { job_id: jobId } }));
  }

  /** Fetches only the missing bars for the members over a window (a background job). */
  ensure(id: string, body: EnsureDataRequest) {
    return unwrap(ensureUniverseData({ path: { universe_id: id }, body }));
  }

  ensureResult(jobId: string) {
    return unwrap(getUniverseEnsureResult({ path: { job_id: jobId } }));
  }
}
