import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { HealthService } from './health.service';
import { provideApi } from './provide-api';

describe('HealthService', () => {
  let controller: HttpTestingController;
  let health: HealthService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    health = TestBed.inject(HealthService);
  });

  afterEach(() => controller.verify());

  it('runs the checks now, for chosen tickers or the production universe', async () => {
    const chosen = health.runChecks(['AAPL.US']);
    const post = await nextRequest(controller, '/api/health/run', 'POST');
    expect(post.request.body).toEqual({ tickers: ['AAPL.US'] });
    post.flush({ healthy: true, checks: [], checked_at: '2026-09-26T12:00:00Z' });
    expect((await chosen).healthy).toBe(true);

    const all = health.runChecks([]);
    const again = await nextRequest(controller, '/api/health/run', 'POST');
    expect(again.request.body).toEqual({ tickers: null });
    again.flush({ healthy: false, checks: [], checked_at: '2026-09-26T12:00:00Z' });
    await all;
  });
});
