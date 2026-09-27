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

  it('reads the survival presets', async () => {
    const presets = lab.survivalPresets();
    (await nextRequest(controller, '/api/lab/survival-presets')).flush([
      { name: 'quick', tests: ['oos'], options: {} },
    ]);
    expect((await presets)[0].tests).toEqual(['oos']);
  });

  it('reads the trial ledger, one page and one run', async () => {
    const page = lab.ledgerRuns({ strategy_class: 'x:Y', limit: 25, offset: 50 });
    const req = await nextRequest(controller, '/api/lab/ledger');
    expect(req.request.urlWithParams).toContain('strategy_class=x%3AY');
    expect(req.request.urlWithParams).toContain('offset=50');
    req.flush({ items: [], total: 0, limit: 25, offset: 50 });
    expect((await page).total).toBe(0);

    const run = lab.ledgerRun('lab_1');
    (await nextRequest(controller, '/api/lab/ledger/lab_1')).flush({ id: 'lab_1', trials: [] });
    expect((await run).id).toBe('lab_1');
  });
});
