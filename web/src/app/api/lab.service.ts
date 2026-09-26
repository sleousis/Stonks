import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getBacktestResult,
  getLabRunResult,
  getSweepResult,
  listCostModels,
  listSurvivalTests,
  startBacktest,
  startLabRun,
  startSweep,
} from './generated/sdk.gen';
import type { BacktestRequest, LabRunRequest, SweepRequest } from './models';

/** Backtests, lab runs and sweeps (background jobs), their results and the test catalog. */
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

  startSweep(body: SweepRequest) {
    return unwrap(startSweep({ body }));
  }

  sweepResult(jobId: string) {
    return unwrap(getSweepResult({ path: { job_id: jobId } }));
  }

  /** Every survival test with the JSON Schema of its options. */
  survivalTests() {
    return unwrap(listSurvivalTests());
  }

  costModels() {
    return unwrap(listCostModels());
  }
}
