import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { provideApi } from './provide-api';
import { SignalsService } from './signals.service';

describe('SignalsService', () => {
  let controller: HttpTestingController;
  let signals: SignalsService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    signals = TestBed.inject(SignalsService);
  });

  afterEach(() => controller.verify());

  it('queues a signal IC job and reads its result', async () => {
    const job = signals.startSignalIc({
      strategy: { class_path: 'pkg.mod:Momentum' },
      universe: ['AAPL.US'],
      start: '2025-01-01',
      end: '2025-12-31',
    });
    (await nextRequest(controller, '/api/lab/signal-ic', 'POST')).flush(
      { id: 'j1', kind: 'signal_ic', status: 'queued' },
      { status: 202, statusText: 'Accepted' },
    );
    expect((await job).id).toBe('j1');
    const result = signals.signalIcResult('j1');
    (await nextRequest(controller, '/api/lab/signal-ic/j1/result')).flush({
      strategy_id: 'momentum',
      status: 'ok',
      ic_estimate: 0.03,
    });
    expect((await result).ic_estimate).toBe(0.03);
  });
});
