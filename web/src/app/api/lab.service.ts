import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getBacktestResult,
  getLabRunResult,
  getLedgerRun,
  getSweepResult,
  listCostModels,
  listLedgerRuns,
  listSurvivalPresets,
  listSurvivalTests,
  startBacktest,
  startLabRun,
  startSweep,
} from './generated/sdk.gen';
import type { BacktestRequest, LabRunRequest, ListLedgerRunsData, SweepRequest } from './models';

/**
 * Backtests, lab runs and sweeps (background jobs), their results, the test
 * catalog and presets, and the trial ledger.
 */
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

  /** The named suites (quick, standard, promotion) and the tests in each. */
  survivalPresets() {
    return unwrap(listSurvivalPresets());
  }

  costModels() {
    return unwrap(listCostModels());
  }

  /** Recorded lab runs, newest first (one page; filter by strategy class). */
  ledgerRuns(query?: ListLedgerRunsData['query']) {
    return unwrap(listLedgerRuns({ query }));
  }

  /** One recorded lab run with every trial and its class's trial count. */
  ledgerRun(runId: string) {
    return unwrap(getLedgerRun({ path: { run_id: runId } }));
  }
}
