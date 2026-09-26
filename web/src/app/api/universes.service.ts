import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import {
  createUniverse,
  deleteUniverse,
  ensureUniverseData,
  getUniverse,
  getUniverseEnsureResult,
  getUniverseMembers,
  getUniverseRefreshResult,
  importIndexHistory,
  listUniverses,
  refreshUniverse,
} from './generated/sdk.gen';
import type { EnsureDataRequest, IndexHistoryImport, UniverseCreate } from './models';

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
