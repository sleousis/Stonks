import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { provideApi } from './provide-api';
import { ScheduleService } from './schedule.service';

describe('ScheduleService', () => {
  let controller: HttpTestingController;
  let schedule: ScheduleService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    schedule = TestBed.inject(ScheduleService);
  });

  afterEach(() => controller.verify());

  it('lists jobs and recent runs', async () => {
    const overview = schedule.overview({ limit: 10 });
    const req = await nextRequest(controller, '/api/schedule');
    expect(req.request.urlWithParams).toContain('limit=10');
    req.flush({ backend: 'in_process', hosted: true, jobs: [], recent: [] });
    expect((await overview).hosted).toBe(true);
  });

  it('runs a job now', async () => {
    const started = schedule.runNow('tick', { as_of: '2026-09-25' });
    const req = await nextRequest(controller, '/api/schedule/tick/run-now', 'POST');
    expect(req.request.body).toEqual({ as_of: '2026-09-25' });
    req.flush({ job: 'tick', run_key: 'manual:x', as_of: '2026-09-25', status: 'started' });
    expect((await started).run_key).toBe('manual:x');
  });
});
