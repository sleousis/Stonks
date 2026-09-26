import { type DestroyRef, signal } from '@angular/core';

import type { JobHandle, JobsService } from '../../core/jobs/jobs.service';

/**
 * Follows one background job at a time and loads its result when it
 * succeeds. A result that fails to load is kept in `error` so the page can
 * show it inline with Retry (the error interceptor does not toast GETs).
 */
export class JobFollower<T> {
  readonly handle = signal<JobHandle | null>(null);
  readonly jobId = signal<string | null>(null);
  readonly result = signal<T | null>(null);
  readonly loading = signal(false);
  readonly error = signal<unknown>(null);

  constructor(
    private readonly jobs: JobsService,
    private readonly destroyRef: DestroyRef,
    private readonly load: (jobId: string) => Promise<T>,
  ) {}

  /** Track `jobId`; resolves once it ended and its result loaded (or failed to). */
  async follow(jobId: string): Promise<void> {
    this.handle()?.stop();
    const handle = this.jobs.track(jobId, this.destroyRef);
    this.jobId.set(jobId);
    this.handle.set(handle);
    this.result.set(null);
    this.error.set(null);
    const last = await handle.finished;
    if (this.jobId() !== jobId) return;
    if (last?.status === 'succeeded') await this.loadResult(jobId);
  }

  /** Load the result again after a failed load. */
  retry(): void {
    const id = this.jobId();
    if (id) void this.loadResult(id);
  }

  private async loadResult(jobId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const result = await this.load(jobId);
      if (this.jobId() === jobId) this.result.set(result);
    } catch (err) {
      if (this.jobId() === jobId) this.error.set(err);
    } finally {
      this.loading.set(false);
    }
  }
}
