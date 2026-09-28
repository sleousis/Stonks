import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import {
  checkModelSwap,
  getModelCalibration,
  getModelRetrainResult,
  getModelVersionHistory,
  listModelCandidates,
  listModelVersions,
  rejectModelVersion,
  startModelRetrain,
  swapModelVersion,
} from './generated/sdk.gen';
import type { RetrainRequest, VersionChangeRequest } from './generated/types.gen';

export type {
  CalibrationView,
  ModelVersionView,
  RetrainOutcomeView,
  RetrainRequest,
  RetrainResultView,
  SwapCheckView,
  SwapReportView,
  VersionChangeRequest,
  VersionEventView,
} from './generated/types.gen';

/**
 * Model versions under one strategy id (roadmap 22.6, docs/model-lifecycle.md).
 * A retrain saves each new fit as a candidate that runs as a model book. A
 * swap makes it live through the swap check, or an override with a reason
 * of at least 20 characters. Swap and reject need `strategy.promote`.
 */
@Injectable({ providedIn: 'root' })
export class ModelVersionsService {
  /** The strategy's versions, oldest first. */
  list(strategyId: string) {
    return allItems((query) =>
      unwrap(listModelVersions({ path: { strategy_id: strategyId }, query })),
    );
  }

  /** The append-only version log, oldest first. */
  history(strategyId: string) {
    return allItems((query) =>
      unwrap(getModelVersionHistory({ path: { strategy_id: strategyId }, query })),
    );
  }

  /** Every candidate across strategies. */
  candidates() {
    return allItems((query) => unwrap(listModelCandidates({ query })));
  }

  /** The swap check of a candidate against the live version. Reports only. */
  check(strategyId: string, version: number) {
    return unwrap(checkModelSwap({ path: { strategy_id: strategyId, version } }));
  }

  /** Live calibration of a classifier version: Brier score and reliability (roadmap 23.9). */
  calibration(strategyId: string, version: number) {
    return unwrap(getModelCalibration({ path: { strategy_id: strategyId, version } }));
  }

  /** Make the candidate live. 409 when the check fails and there is no override. */
  swap(strategyId: string, version: number, body: VersionChangeRequest) {
    return unwrap(swapModelVersion({ path: { strategy_id: strategyId, version }, body }));
  }

  /** Drop a candidate, with a reason. */
  reject(strategyId: string, version: number, reason: string) {
    return unwrap(
      rejectModelVersion({ path: { strategy_id: strategyId, version }, body: { reason } }),
    );
  }

  /** Queue a retrain job. Follow it, then read `retrainResult`. */
  retrain(body: RetrainRequest) {
    return unwrap(startModelRetrain({ body }));
  }

  retrainResult(jobId: string) {
    return unwrap(getModelRetrainResult({ path: { job_id: jobId } }));
  }
}
