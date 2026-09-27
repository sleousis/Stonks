import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest, page } from '../../testing/http';
import { HaltsService, RESUME_CONFIRMATION } from './halts.service';
import { provideApi } from './provide-api';

describe('HaltsService', () => {
  let controller: HttpTestingController;
  let halts: HaltsService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    halts = TestBed.inject(HaltsService);
  });

  afterEach(() => controller.verify());

  it('lists active halts, or all of them', async () => {
    const active = halts.list();
    const req = await nextRequest(controller, '/api/halts');
    expect(req.request.urlWithParams).toContain('include_cleared=false');
    req.flush(page([]));
    expect(await active).toEqual([]);

    const all = halts.list(true);
    const req2 = await nextRequest(controller, '/api/halts');
    expect(req2.request.urlWithParams).toContain('include_cleared=true');
    req2.flush(page([]));
    await all;
  });

  it('engages, resumes and clears', async () => {
    const kill = halts.kill({ scope: 'global', reason: 'outage', buys_only: false });
    const k = await nextRequest(controller, '/api/halts/kill', 'POST');
    expect(k.request.body).toEqual({ scope: 'global', reason: 'outage', buys_only: false });
    k.flush({ id: 4 });
    await kill;

    const resume = halts.resume(4, { confirmation: RESUME_CONFIRMATION, reason: 'ok' });
    const r = await nextRequest(controller, '/api/halts/4/resume', 'POST');
    expect(r.request.body).toEqual({ confirmation: 'RESUME TRADING', reason: 'ok' });
    r.flush({ id: 4 });
    await resume;

    const clear = halts.clear(5, { reason: 'reviewed' });
    const c = await nextRequest(controller, '/api/halts/5/clear', 'POST');
    expect(c.request.body).toEqual({ reason: 'reviewed' });
    c.flush({ id: 5 });
    await clear;
  });
});
