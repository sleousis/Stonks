import {
  DestroyRef,
  Injectable,
  InjectionToken,
  type Signal,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Observable, type Subscriber } from 'rxjs';

import { JobsApiService } from '../../api/jobs-api.service';
import { type Job, type JobEvent, type JobStatus, TERMINAL_JOB_STATUSES } from '../../api/models';
import { AuthTokenService } from '../auth/auth-token.service';
import { SseParser } from './sse-parser';

/** Minimal fetch surface the job stream needs (swapped for a fake in tests). */
export type FetchLike = (
  input: string,
  init: {
    headers: Record<string, string>;
    credentials: RequestCredentials;
    signal: AbortSignal;
    cache: RequestCache;
  },
) => Promise<{
  ok: boolean;
  status: number;
  body: { getReader(): ReadableStreamDefaultReader<Uint8Array> } | null;
}>;

export const JOB_FETCH = new InjectionToken<FetchLike>('JOB_FETCH', {
  providedIn: 'root',
  factory: () => (input, init) => globalThis.fetch(input, init),
});

/** Poll interval when the event stream is unavailable. */
export const JOB_POLL_MS = new InjectionToken<number>('JOB_POLL_MS', {
  providedIn: 'root',
  factory: () => 1000,
});

/** Live view of one job, for templates. `progress` is 0..1. */
export interface JobHandle {
  readonly jobId: string;
  readonly event: Signal<JobEvent | null>;
  readonly status: Signal<JobStatus | null>;
  readonly progress: Signal<number>;
  readonly message: Signal<string | null>;
  readonly error: Signal<string | null>;
  readonly done: Signal<boolean>;
  /** Resolves with the last event once the job ends (or the stream gives up). */
  readonly finished: Promise<JobEvent | null>;
  stop(): void;
}

const MAX_POLL_FAILURES = 3;

export function isTerminal(status: JobStatus | undefined | null): boolean {
  return !!status && TERMINAL_JOB_STATUSES.includes(status);
}

/**
 * Follows background jobs (backtests, lab runs, ingest, ticks).
 *
 * Start the job through its domain service, then track it:
 *
 *   const job = await this.lab.startBacktest(request);
 *   this.run.set(this.jobs.track(job.id, this.destroyRef));
 *   // template: {{ run()?.progress() | percent }}
 *
 * Progress arrives over the SSE stream (`GET /api/jobs/{id}/events`), read
 * with fetch so the short-lived stream token from `POST .../stream-token`
 * goes in the URL and the bearer token never does. Without an API token the
 * stream is opened directly with the session cookie. If the stream
 * cannot be opened or stops early, the service polls `GET /api/jobs/{id}`.
 */
@Injectable({ providedIn: 'root' })
export class JobsService {
  private readonly api = inject(JobsApiService);
  private readonly auth = inject(AuthTokenService);
  private readonly fetchFn = inject(JOB_FETCH);
  private readonly pollMs = inject(JOB_POLL_MS);

  /** Events for a job until it reaches a terminal status; then completes. */
  watch(jobId: string): Observable<JobEvent> {
    return new Observable<JobEvent>((subscriber) => {
      const controller = new AbortController();
      this.follow(jobId, subscriber, controller.signal).then(
        () => subscriber.complete(),
        (err: unknown) => {
          if (!controller.signal.aborted) subscriber.error(err);
        },
      );
      return () => controller.abort();
    });
  }

  /** Signal-based handle for templates; stops when `destroyRef` is destroyed. */
  track(jobId: string, destroyRef?: DestroyRef): JobHandle {
    const event = signal<JobEvent | null>(null);
    const failure = signal<string | null>(null);
    const ended = signal(false);
    let resolveFinished!: (e: JobEvent | null) => void;
    const finished = new Promise<JobEvent | null>((r) => (resolveFinished = r));

    const sub = this.watch(jobId).subscribe({
      next: (e) => event.set(e),
      error: (err: unknown) => {
        failure.set(err instanceof Error ? err.message : 'Lost track of the job.');
        ended.set(true);
        resolveFinished(event());
      },
      complete: () => {
        // Only an `untracked` end completes before a terminal status: no
        // worker will finish this job, so say so rather than "Running…".
        const last = event();
        if (last && !isTerminal(last.status)) {
          failure.set('The server lost track of this job. Run it again.');
        }
        ended.set(true);
        resolveFinished(last);
      },
    });
    // Stopping settles `finished` with the last event, so nobody waits forever.
    const stop = () => {
      sub.unsubscribe();
      ended.set(true);
      resolveFinished(event());
    };
    destroyRef?.onDestroy(stop);

    return {
      jobId,
      event: event.asReadonly(),
      status: computed(() => event()?.status ?? null),
      progress: computed(() => event()?.progress ?? 0),
      message: computed(() => event()?.message ?? null),
      error: computed(() => failure() ?? event()?.error ?? null),
      done: ended.asReadonly(),
      finished,
      stop,
    };
  }

  private async follow(
    jobId: string,
    subscriber: Subscriber<JobEvent>,
    signal: AbortSignal,
  ): Promise<void> {
    let finished = false;
    try {
      finished = await this.stream(jobId, subscriber, signal);
    } catch {
      // Stream unavailable (no token, proxy without SSE, network): poll instead.
    }
    if (finished || signal.aborted) return;
    await this.poll(jobId, subscriber, signal);
  }

  /** Returns true when the stream delivered the job's final event. */
  private async stream(
    jobId: string,
    subscriber: Subscriber<JobEvent>,
    signal: AbortSignal,
  ): Promise<boolean> {
    let url = `/api/jobs/${encodeURIComponent(jobId)}/events`;
    if (this.auth.hasToken()) {
      url = (await this.api.streamToken(jobId)).events_url;
    }
    const res = await this.fetchFn(url, {
      headers: { Accept: 'text/event-stream' },
      credentials: 'include',
      signal,
      cache: 'no-store',
    });
    if (!res.ok || !res.body) throw new Error(`event stream unavailable (${res.status})`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return false;
        for (const msg of parser.push(decoder.decode(value, { stream: true }))) {
          if (msg.event !== 'status' && msg.event !== 'done' && msg.event !== 'end') continue;
          const event = JSON.parse(msg.data) as JobEvent;
          subscriber.next(event);
          if (msg.event === 'done') return true;
          if (msg.event === 'end') {
            // `timeout`: the job is still running, keep following by polling.
            // `untracked`: no worker will update it again; stop here.
            return event.reason === 'untracked';
          }
        }
      }
    } finally {
      reader.releaseLock?.();
    }
  }

  private async poll(
    jobId: string,
    subscriber: Subscriber<JobEvent>,
    signal: AbortSignal,
  ): Promise<void> {
    let last = '';
    let failures = 0;
    while (!signal.aborted) {
      try {
        const job = await this.api.get(jobId, true);
        failures = 0;
        const event = toEvent(job);
        const key = `${event.status}|${event.progress}|${event.message ?? ''}`;
        if (key !== last) {
          last = key;
          subscriber.next(event);
        }
        if (isTerminal(job.status)) return;
      } catch (err) {
        if (++failures >= MAX_POLL_FAILURES) throw err;
      }
      await sleep(this.pollMs, signal);
    }
  }
}

function toEvent(job: Job): JobEvent {
  return {
    job_id: job.id,
    status: job.status,
    progress: job.progress,
    message: job.message ?? null,
    error: job.error ?? null,
  };
}

/** Waits `ms`, or less when aborted. Leaves no abort listener behind. */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const onAbort = () => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}
