import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { JobStatus } from '../../api/models';
import type { JobHandle } from '../../core/jobs/jobs.service';
import { JobProgress, jobText } from './job-progress';

function handle(status: JobStatus | null, extra: { message?: string; error?: string } = {}) {
  const done = status === 'succeeded' || status === 'failed' || status === 'cancelled';
  return {
    jobId: 'job_1',
    event: signal(null),
    status: signal(status),
    progress: signal(0.4),
    message: signal(extra.message ?? null),
    error: signal(extra.error ?? null),
    done: signal(done),
    finished: Promise.resolve(null),
    stop: () => undefined,
  } as JobHandle;
}

describe('JobProgress', () => {
  it('describes each state in plain words', () => {
    expect(jobText(handle(null))).toBe('Waiting for a worker…');
    expect(jobText(handle('running', { message: 'Copying the lake' }))).toBe('Copying the lake');
    expect(jobText(handle('running'))).toBe('Running…');
    expect(jobText(handle('succeeded'))).toBe('Finished.');
    expect(jobText(handle('failed'))).toBe('Failed.');
  });

  it('shows progress while running and the error when it fails', () => {
    const fixture = TestBed.createComponent(JobProgress);
    fixture.componentRef.setInput('label', 'Backup');
    fixture.componentRef.setInput('handle', handle('running'));
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Backup: Running…');
    expect(el.querySelector('progress')!.value).toBeCloseTo(0.4);

    fixture.componentRef.setInput('handle', handle('failed', { error: 'disk full' }));
    fixture.detectChanges();
    expect(el.querySelector('progress')).toBeNull();
    expect(el.querySelector('[role="alert"]')!.textContent).toContain('disk full');
  });
});
