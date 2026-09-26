import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getBacktestResult,
  getLabRunResult,
  listCostModels,
  startBacktest,
  startLabRun,
} from './generated/sdk.gen';
import type { BacktestRequest, LabRunRequest } from './models';

/** Backtests and lab runs (background jobs) and their results. */
@Injectable({ providedIn: 'root' })
export class LabService {
  startBacktest(body: BacktestRequest) {
    return unwrap(startBacktest({ body }));
  }

  backtestResult(jobId: string) {
    return unwrap(getBacktestResult({ path: { job_id: jobId } }));
  }

  startLabRun(body: LabRunRequest) {
    return unwrap(startLabRun({ body }));
  }

  labRunResult(jobId: string) {
    return unwrap(getLabRunResult({ path: { job_id: jobId } }));
  }

  costModels() {
    return unwrap(listCostModels());
  }
}
