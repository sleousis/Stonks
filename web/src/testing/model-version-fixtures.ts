import { signal } from '@angular/core';

import type {
  ModelVersionView,
  RetrainResultView,
  SwapReportView,
  VersionEventView,
} from '../app/api/model-versions.service';
import type { JobHandle } from '../app/core/jobs/jobs.service';

export const MODEL_ID = 'trendline_meta_label_1a2b3c4d';

export function modelVersion(over: Partial<ModelVersionView> = {}): ModelVersionView {
  const version = over.version ?? 1;
  return {
    strategy_id: MODEL_ID,
    version,
    status: 'live',
    book_id: `${over.strategy_id ?? MODEL_ID}@v${version}`,
    train_start: '2024-01-02',
    train_end: '2025-12-31',
    fit: {},
    error: null,
    created_by: 'user:usr_owner',
    created_at: '2026-01-02T10:00:00Z',
    updated_at: '2026-01-02T10:00:00Z',
    ...over,
  };
}

export const LIVE_V1 = modelVersion();
export const CANDIDATE_V2 = modelVersion({
  version: 2,
  status: 'candidate',
  train_start: '2024-09-01',
  train_end: '2026-09-19',
  created_by: 'service:scheduler',
  created_at: '2026-09-20T06:00:00Z',
});

export function swapReport(passed: boolean, over: Partial<SwapReportView> = {}): SwapReportView {
  return {
    strategy_id: MODEL_ID,
    version: 2,
    live_version: 1,
    passed,
    days: passed ? 24 : 3,
    candidate_return: passed ? 0.031 : -0.012,
    live_return: 0.018,
    candidate_drawdown: 0.04,
    checks: [
      {
        name: 'candidate',
        passed: true,
        value: null,
        limit: null,
        detail: 'candidate of a active strategy',
      },
      {
        name: 'min_days',
        passed,
        value: passed ? 24 : 3,
        limit: 20,
        detail: passed ? '24 model book day(s), need >= 20' : '3 model book day(s), need >= 20',
      },
      {
        name: 'max_drawdown',
        passed: true,
        value: 0.04,
        limit: 0.25,
        detail: 'max drawdown 4.00%, limit 25.00%',
      },
      {
        name: 'vs_live',
        passed,
        value: passed ? 0.8 : -2.4,
        limit: -1.645,
        detail: passed
          ? 'mean daily gap +0.040% vs live v1 over 20 paired days (HAC t +0.80)'
          : 'mean daily gap -0.150% vs live v1 over 20 paired days (HAC t -2.40): trails live',
      },
    ],
    ...over,
  };
}

export function versionEvent(over: Partial<VersionEventView> = {}): VersionEventView {
  return {
    id: 1,
    version: 1,
    kind: 'baseline',
    from_status: null,
    to_status: 'live',
    actor: 'service:model_versions',
    reason: '',
    override: false,
    check_passed: null,
    check_report: null,
    created_at: '2026-01-02T10:00:00Z',
    ...over,
  };
}

export const RETRAIN_RESULT: RetrainResultView = {
  as_of: '2026-09-26',
  candidates: 1,
  failed: 0,
  skipped: 1,
  outcomes: [
    {
      strategy_id: MODEL_ID,
      status: 'candidate',
      version: 3,
      detail: null,
      train_start: '2024-09-26',
      train_end: '2026-09-26',
    },
    {
      strategy_id: 'rsi_pca_9f8e7d6c',
      status: 'skipped',
      version: null,
      detail: 'fitted 2 day(s) ago',
      train_start: null,
      train_end: null,
    },
  ],
};

/** A job handle that has already succeeded. */
export function finishedJob(jobId: string): JobHandle {
  const event = { job_id: jobId, status: 'succeeded', progress: 1 } as const;
  return {
    jobId,
    event: signal(event),
    status: signal('succeeded'),
    progress: signal(1),
    message: signal(null),
    error: signal(null),
    done: signal(true),
    finished: Promise.resolve(event),
    stop: () => undefined,
  };
}
