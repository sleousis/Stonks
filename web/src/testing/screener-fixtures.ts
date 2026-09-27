import { signal } from '@angular/core';

import type {
  JobEvent,
  JobStatus,
  MetricView,
  SavedScreenView,
  ScreenResult,
  ScreenSize,
} from '../app/api/models';
import type { JobHandle } from '../app/core/jobs/jobs.service';

/** A few screen metrics, one of each unit. */
export const METRICS: MetricView[] = [
  { id: 'price', label: 'Price', group: 'price', unit: 'money', description: 'Last close.' },
  {
    id: 'return_12m',
    label: '12-month return',
    group: 'price',
    unit: 'percent',
    description: 'Adjusted return over a year.',
  },
  {
    id: 'pe_ratio',
    label: 'P/E',
    group: 'fundamental',
    unit: 'ratio',
    description: 'Price over trailing earnings.',
  },
  {
    id: 'dividend_yield',
    label: 'Dividend yield',
    group: 'fundamental',
    unit: 'percent',
    description: 'Dividends over the last close.',
  },
];

export const SAVED: SavedScreenView = {
  id: 'scr_1',
  name: 'Cheap payers',
  spec: { filters: [{ metric: 'dividend_yield', min: 0.04, max: null }], limit: 50 },
  created_at: '2026-09-20T10:00:00Z',
  updated_at: '2026-09-21T10:00:00Z',
};

export const RESULT: ScreenResult = {
  as_of: '2026-09-25',
  candidates: 120,
  matched: 2,
  metrics: ['dividend_yield', 'price'],
  truncated: false,
  rows: [
    {
      ticker: 'KO.US',
      name: 'Coca-Cola',
      sector: 'Consumer Staples',
      exchange: 'US',
      values: { dividend_yield: 0.031, price: 62.5 },
    },
    {
      ticker: 'T.US',
      name: 'AT&T',
      sector: 'Communication',
      exchange: 'US',
      values: { dividend_yield: 0.065, price: null },
    },
  ],
};

/** A size answer: small by default, `use_job` or `over_cap` when asked. */
export function screenSize(candidates: number, change: Partial<ScreenSize> = {}): ScreenSize {
  return {
    as_of: '2026-09-25',
    candidates,
    max_candidates: 10000,
    job_threshold: 1000,
    over_cap: false,
    use_job: false,
    ...change,
  };
}

/** A followed job the test moves by hand: `step` while it runs, then `end`. */
export function controlledJob(jobId: string) {
  const status = signal<JobStatus | null>('queued');
  const progress = signal(0);
  const message = signal<string | null>(null);
  const error = signal<string | null>(null);
  const done = signal(false);
  const event = signal<JobEvent | null>(null);
  let resolve!: (e: JobEvent | null) => void;
  const finished = new Promise<JobEvent | null>((r) => (resolve = r));
  const handle: JobHandle = {
    jobId,
    event,
    status,
    progress,
    message,
    error,
    done,
    finished,
    stop: () => undefined,
  };
  return {
    handle,
    step(fraction: number, text: string) {
      status.set('running');
      progress.set(fraction);
      message.set(text);
    },
    end(final: JobStatus, failure: string | null = null) {
      const e: JobEvent = { job_id: jobId, status: final, progress: 1, error: failure };
      status.set(final);
      error.set(failure);
      done.set(true);
      event.set(e);
      resolve(e);
    },
  };
}
