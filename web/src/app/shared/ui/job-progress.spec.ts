import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { JobStatus } from '../../api/models';
import type { JobHandle } from '../../core/jobs/jobs.service';
import { JobProgress, JobResult, jobText } from './job-progress';

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

  it('shows a failure once, in the job line', () => {
    const fixture = TestBed.createComponent(JobProgress);
    fixture.componentRef.setInput('label', 'Data update');
    fixture.componentRef.setInput('handle', handle('failed', { error: 'vendor down' }));
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent!.match(/vendor down/g)?.length).toBe(1);
  });

  it('projects a Cancel slot next to the status', () => {
    @Component({
      imports: [JobProgress],
      template: `<app-job-progress label="Data update" [handle]="h"
        ><button jobActions type="button">Cancel</button></app-job-progress
      >`,
    })
    class Host {
      h = handle('queued');
    }
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    const line = (fixture.nativeElement as HTMLElement).querySelector('.job-line')!;
    expect(line.querySelector('button')?.textContent).toBe('Cancel');
  });

  it('result 500 after success shows an inline error with retry', async () => {
    const result = new JobResult<string>();
    const load = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new Error('Result store unavailable.'))
      .mockResolvedValueOnce('ok');
    const fixture = TestBed.createComponent(JobProgress);
    fixture.componentRef.setInput('label', 'Backup');
    fixture.componentRef.setInput('handle', handle('succeeded'));
    fixture.componentRef.setInput('result', result);
    expect(await result.load(load)).toBeNull();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const error = el.querySelector('app-error-state')!;
    expect(error.textContent).toContain('Backup finished, but its result could not load');
    expect(error.textContent).toContain('Result store unavailable.');

    error.querySelector('button')!.click();
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.value()).toBe('ok');
    expect(el.querySelector('app-error-state')).toBeNull();
  });
});
