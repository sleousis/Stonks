import { type DestroyRef, signal } from '@angular/core';

import type { JobEvent } from '../../api/models';
import type { JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { tick } from '../../../testing/http';
import { JobFollower } from './job-follower';

/** A job handle whose end the test decides. */
function handle(jobId: string) {
  let end!: (e: JobEvent | null) => void;
  const finished = new Promise<JobEvent | null>((resolve) => (end = resolve));
  const stop = vi.fn();
  const h: JobHandle = {
    jobId,
    event: signal(null),
    status: signal(null),
    progress: signal(0),
    message: signal(null),
    error: signal(null),
    done: signal(false),
    finished,
    stop,
  };
  const succeed = () => end({ job_id: jobId, status: 'succeeded', progress: 1 } as JobEvent);
  const fail = () => end({ job_id: jobId, status: 'failed', progress: 1 } as JobEvent);
  return { h, succeed, fail, stop };
}

/** A promise the test resolves or rejects. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('JobFollower', () => {
  const destroyRef = {} as DestroyRef;
  let handles: Record<string, ReturnType<typeof handle>>;
  let loads: Record<string, ReturnType<typeof deferred<string>>>;
  let follower: JobFollower<string>;

  beforeEach(() => {
    handles = {};
    loads = {};
    const jobs = {
      track: (id: string) => (handles[id] = handle(id)).h,
    } as unknown as JobsService;
    follower = new JobFollower<string>(jobs, destroyRef, (id) => {
      loads[id] = deferred<string>();
      return loads[id].promise;
    });
  });

  it('loads the result once the job succeeds', async () => {
    const done = follower.follow('a');
    expect(follower.jobId()).toBe('a');
    expect(follower.handle()).toBe(handles['a'].h);
    handles['a'].succeed();
    await tick();
    expect(follower.loading()).toBe(true);
    loads['a'].resolve('result A');
    await done;
    expect(follower.result()).toBe('result A');
    expect(follower.loading()).toBe(false);
  });

  it('loads nothing when the job fails', async () => {
    const done = follower.follow('a');
    handles['a'].fail();
    await done;
    expect(loads['a']).toBeUndefined();
    expect(follower.result()).toBeNull();
  });

  it('keeps a failed load in error, and Retry loads it again', async () => {
    const done = follower.follow('a');
    handles['a'].succeed();
    await tick();
    loads['a'].reject(new Error('boom'));
    await done;
    expect(follower.error()).toBeInstanceOf(Error);

    follower.retry();
    expect(follower.error()).toBeNull();
    loads['a'].resolve('result A');
    await tick();
    expect(follower.result()).toBe('result A');
  });

  it('stops the previous handle when following a new job', () => {
    void follower.follow('a');
    void follower.follow('b');
    expect(handles['a'].stop).toHaveBeenCalled();
    expect(follower.jobId()).toBe('b');
  });

  it('A resolves after opening B: B still shows loading (UX-62)', async () => {
    void follower.follow('a');
    handles['a'].succeed();
    await tick();
    expect(follower.loading()).toBe(true);

    const b = follower.follow('b');
    handles['b'].succeed();
    await tick();
    loads['a'].resolve('stale A');
    await tick();
    expect(follower.loading()).toBe(true);
    expect(follower.result()).toBeNull();

    loads['b'].resolve('result B');
    await b;
    expect(follower.loading()).toBe(false);
    expect(follower.result()).toBe('result B');
  });
});
