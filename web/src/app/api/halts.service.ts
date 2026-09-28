import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  clearHalt,
  engageKillSwitch,
  getHalt,
  getResumeChecks,
  listHalts,
  resumeKillSwitch,
} from './generated/sdk.gen';
import type { ClearHaltRequest, KillSwitchRequest, ResumeRequest } from './models';

/** The exact text `POST /api/halts/{id}/resume` needs. */
export const RESUME_CONFIRMATION = 'RESUME TRADING';

/** Trading halts: the kill switch, circuit-breaker trips and the operational halt. */
@Injectable({ providedIn: 'root' })
export class HaltsService {
  /** Active halts, or every halt with `includeCleared`. `silent` skips error toasts (polling). */
  list(includeCleared = false, silent = false) {
    return allItems((page) =>
      unwrap(
        listHalts({
          query: { include_cleared: includeCleared, ...page },
          headers: silent ? SILENT_HEADERS : undefined,
        }),
      ),
    );
  }

  get(id: number) {
    return unwrap(getHalt({ path: { halt_id: id } }));
  }

  kill(body: KillSwitchRequest) {
    return unwrap(engageKillSwitch({ body }));
  }

  /** What a resume checks first (gateway, last reconcile, account, equity cover). Read only. */
  resumeChecks(id: number) {
    return unwrap(getResumeChecks({ path: { halt_id: id }, headers: SILENT_HEADERS }));
  }

  /** Needs a fresh second factor (step-up); `confirmation` must be `RESUME TRADING`. */
  resume(id: number, body: ResumeRequest) {
    return unwrap(resumeKillSwitch({ path: { halt_id: id }, body }));
  }

  /** Clear a circuit-breaker or operational halt. */
  clear(id: number, body: ClearHaltRequest) {
    return unwrap(clearHalt({ path: { halt_id: id }, body }));
  }
}
