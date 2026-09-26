import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getSignalIcResult, startSignalIc } from './generated/sdk.gen';
import type { SignalIcRequest } from './models';

/** Signal research: IC analysis of a strategy's scores (a background job). */
@Injectable({ providedIn: 'root' })
export class SignalsService {
  startSignalIc(body: SignalIcRequest) {
    return unwrap(startSignalIc({ body }));
  }

  signalIcResult(jobId: string) {
    return unwrap(getSignalIcResult({ path: { job_id: jobId } }));
  }
}
