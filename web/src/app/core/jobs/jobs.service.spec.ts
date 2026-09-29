import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom, toArray } from 'rxjs';

import type { Job, JobEvent } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { AuthTokenService } from '../auth/auth-token.service';
import { type FetchLike, JOB_FETCH, JOB_POLL_MS, JobsService } from './jobs.service';

function sseBody(chunks: string[]) {
  const encoder = new TextEncoder();
  const queue = [...chunks];
  return {
    getReader: () =>
      ({
        read: async () => {
          const next = queue.shift();
          return next === undefined
            ? { done: true, value: undefined }
            : { done: false, value: encoder.encode(next) };
        },
        releaseLock: () => undefined,
      }) as unknown as ReadableStreamDefaultReader<Uint8Array>,
  };
}

function event(name: string, e: Partial<JobEvent>): string {
  const data = { job_id: 'j1', status: 'running', progress: 0, ...e };
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
}

function job(partial: Partial<Job>): Job {
  return {
    id: 'j1',
    kind: 'backtest',
    params: {},
    status: 'running',
    progress: 0,
    created_at: '2026-09-26T10:00:00Z',
    ...partial,
  };
}

describe('JobsService', () => {
  let fetchMock: ReturnType<typeof vi.fn<FetchLike>>;
  let controller: HttpTestingController;
  let jobs: JobsService;

  beforeEach(() => {
    sessionStorage.clear();
    fetchMock = vi.fn<FetchLike>();
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: JOB_FETCH, useValue: fetchMock },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    jobs = TestBed.inject(JobsService);
  });

  afterEach(() => controller.verify());

  it('streams events without a token and completes on "done"', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      body: sseBody([
        event('status', { progress: 0.25, message: 'trial 1/4' }),
        event('status', { progress: 0.5 }).slice(0, 20),
        event('status', { progress: 0.5 }).slice(20),
        event('done', { status: 'succeeded', progress: 1 }),
      ]),
    });

    const events = await firstValueFrom(jobs.watch('j1').pipe(toArray()));

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/jobs/j1/events',
      expect.objectContaining({ headers: { Accept: 'text/event-stream' } }),
    );
    expect(events.map((e) => [e.status, e.progress])).toEqual([
      ['running', 0.25],
      ['running', 0.5],
      ['succeeded', 1],
    ]);
    expect(events[0].message).toBe('trial 1/4');
  });

  it('uses a stream token (never the bearer token) in the events URL when a token is set', async () => {
    TestBed.inject(AuthTokenService).setToken('bearer-secret');
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      body: sseBody([event('done', { status: 'succeeded', progress: 1 })]),
    });

    const done = firstValueFrom(jobs.watch('j1').pipe(toArray()));
    const tokenReq = await nextRequest(controller, '/api/jobs/j1/stream-token', 'POST');
    expect(tokenReq.request.headers.get('Authorization')).toBe('Bearer bearer-secret');
    tokenReq.flush({
      token: 'short',
      expires_at: '2026-09-26T10:05:00Z',
      events_url: '/api/jobs/j1/events?token=short',
    });

    await done;
    const url = fetchMock.mock.calls[0][0];
    expect(url).toBe('/api/jobs/j1/events?token=short');
    expect(url).not.toContain('bearer-secret');
  });

  it('falls back to polling when the stream cannot be opened', async () => {
    fetchMock.mockRejectedValue(new TypeError('network'));

    const done = firstValueFrom(jobs.watch('j1').pipe(toArray()));
    (await nextRequest(controller, '/api/jobs/j1')).flush(job({ progress: 0.4 }));
    (await nextRequest(controller, '/api/jobs/j1')).flush(job({ progress: 0.4 }));
    (await nextRequest(controller, '/api/jobs/j1')).flush(
      job({ status: 'succeeded', progress: 1 }),
    );

    const events = await done;
    // The unchanged second poll is not re-emitted.
    expect(events.map((e) => [e.status, e.progress])).toEqual([
      ['running', 0.4],
      ['succeeded', 1],
    ]);
  });

  it('keeps following by polling after a stream timeout', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      body: sseBody([
        event('status', { progress: 0.1 }),
        event('end', { progress: 0.1, reason: 'timeout' }),
      ]),
    });

    const done = firstValueFrom(jobs.watch('j1').pipe(toArray()));
    (await nextRequest(controller, '/api/jobs/j1')).flush(job({ status: 'failed', error: 'boom' }));

    const events = await done;
    expect(events.at(-1)).toMatchObject({ status: 'failed', error: 'boom' });
  });

  it('exposes progress as signals through track()', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      body: sseBody([
        event('status', { progress: 0.5, message: 'half' }),
        event('done', { status: 'succeeded', progress: 1 }),
      ]),
    });

    const handle = jobs.track('j1');
    const last = await handle.finished;
    await tick();

    expect(last?.status).toBe('succeeded');
    expect(handle.status()).toBe('succeeded');
    expect(handle.progress()).toBe(1);
    expect(handle.done()).toBe(true);
    expect(handle.error()).toBeNull();
  });

  it('says so when the server stops following a job that never finished', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      body: sseBody([
        event('status', { progress: 0.2 }),
        event('end', { progress: 0.2, reason: 'untracked' }),
      ]),
    });

    const handle = jobs.track('j1');
    await handle.finished;
    await tick();

    expect(handle.done()).toBe(true);
    expect(handle.error()).toContain('lost track');
  });

  it('reports an error after repeated poll failures', async () => {
    fetchMock.mockRejectedValue(new TypeError('network'));
    const handle = jobs.track('j1');
    for (let i = 0; i < 3; i++) {
      (await nextRequest(controller, '/api/jobs/j1')).flush(
        { title: 'Not Found', status: 404, detail: 'job not found' },
        { status: 404, statusText: 'Not Found' },
      );
    }
    await handle.finished;
    expect(handle.done()).toBe(true);
    expect(handle.error()).toBe('job not found');
  });

  it('polls with the credential (UI-02)', async () => {
    TestBed.inject(AuthTokenService).setToken('bearer-secret');
    fetchMock.mockRejectedValue(new TypeError('network'));
    const done = firstValueFrom(jobs.watch('j1').pipe(toArray()));
    (await nextRequest(controller, '/api/jobs/j1/stream-token', 'POST')).flush(
      { title: 'Gone', status: 404 },
      { status: 404, statusText: 'Not Found' },
    );
    const poll = await nextRequest(controller, '/api/jobs/j1');
    expect(poll.request.headers.get('Authorization')).toBe('Bearer bearer-secret');
    expect(poll.request.withCredentials).toBe(true);
    poll.flush(job({ status: 'succeeded', progress: 1 }));
    await done;
  });

  it('opens the stream with the session cookie', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      body: sseBody([event('done', { status: 'succeeded', progress: 1 })]),
    });
    await firstValueFrom(jobs.watch('j1').pipe(toArray()));
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'include' });
  });

  it('stop() settles finished with the last event (UI-19)', async () => {
    fetchMock.mockRejectedValue(new TypeError('network'));
    const handle = jobs.track('j1');
    (await nextRequest(controller, '/api/jobs/j1')).flush(job({ progress: 0.3 }));
    await tick();
    handle.stop();
    const last = await handle.finished;
    expect(last?.progress).toBe(0.3);
    expect(handle.done()).toBe(true);
    controller.match('/api/jobs/j1').forEach((r) => r.flush(job({ progress: 0.3 })));
  });

  it('polling leaves no abort listeners behind (UI-19)', async () => {
    const added = vi.spyOn(AbortSignal.prototype, 'addEventListener');
    const removed = vi.spyOn(AbortSignal.prototype, 'removeEventListener');
    fetchMock.mockRejectedValue(new TypeError('network'));
    const done = firstValueFrom(jobs.watch('j1').pipe(toArray()));
    for (let i = 0; i < 3; i++) {
      (await nextRequest(controller, '/api/jobs/j1')).flush(job({ progress: i / 10 }));
    }
    (await nextRequest(controller, '/api/jobs/j1')).flush(
      job({ status: 'succeeded', progress: 1 }),
    );
    await done;
    const abortAdds = added.mock.calls.filter(([type]) => type === 'abort').length;
    const abortRemoves = removed.mock.calls.filter(([type]) => type === 'abort').length;
    expect(abortAdds).toBeGreaterThan(0);
    expect(abortRemoves).toBe(abortAdds);
    added.mockRestore();
    removed.mockRestore();
  });
});
