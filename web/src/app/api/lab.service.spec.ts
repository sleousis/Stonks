import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { LabService } from './lab.service';
import { provideApi } from './provide-api';

describe('LabService', () => {
  let controller: HttpTestingController;
  let lab: LabService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    lab = TestBed.inject(LabService);
  });

  afterEach(() => controller.verify());

  it('queues a sweep and reads its rows', async () => {
    const job = lab.startSweep({ start: '2025-01-01', end: '2025-12-31', universe: ['SPY.US'] });
    const post = await nextRequest(controller, '/api/lab/sweeps', 'POST');
    expect(post.request.body).toMatchObject({ universe: ['SPY.US'] });
    post.flush({ id: 'j1', kind: 'lab_sweep', status: 'queued' });
    expect((await job).id).toBe('j1');

    const result = lab.sweepResult('j1');
    (await nextRequest(controller, '/api/lab/sweeps/j1/result')).flush({
      universe: ['SPY.US'],
      rows: [],
      passed: 0,
      failed: 0,
      errors: 0,
    });
    expect((await result).rows).toEqual([]);
  });

  it('lists the survival tests with their option schemas', async () => {
    const list = lab.survivalTests();
    (await nextRequest(controller, '/api/lab/survival-tests')).flush([
      { id: 'oos', description: 'x', options_schema: {}, presets: [] },
    ]);
    expect((await list)[0].id).toBe('oos');
  });
});
