import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { Job } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { IC_EXPLANATION } from '../../shared/lab-results/signal-ic-result';
import { SIGNAL_IC_VIEW } from './lab-test-fixtures';
import { SignalIcPage } from './signal-ic.page';

function job(patch: Partial<Job>): Job {
  return {
    id: 'ic-1',
    kind: 'signal_ic',
    status: 'succeeded',
    progress: 1,
    created_at: '2026-09-26T10:00:00Z',
    params: {},
    ...patch,
  };
}

describe('SignalIcPage', () => {
  let fixture: ComponentFixture<SignalIcPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let posted: unknown[];
  let resultStatus: number;

  beforeEach(async () => {
    posted = [];
    resultStatus = 200;
    TestBed.configureTestingModule({
      imports: [SignalIcPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    controller = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
    fixture = TestBed.createComponent(SignalIcPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
  });

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
    if (req.request.method === 'POST' && path === '/api/lab/signal-ic') {
      posted.push(req.request.body);
      return req.flush(job({ status: 'queued', progress: 0 }));
    }
    switch (path) {
      case '/api/catalog/strategies':
        return req.flush(CATALOG);
      case '/api/catalog/intervals':
        return req.flush([{ code: '1d', is_intraday: false, seconds: 86400 }]);
      case '/api/jobs/ic-1':
        return req.flush(job({}));
      case '/api/lab/signal-ic/ic-1/result':
        return resultStatus === 200
          ? req.flush(SIGNAL_IC_VIEW)
          : req.flush(
              { title: 'x', status: resultStatus, detail: 'Down.' },
              { status: resultStatus, statusText: 'x' },
            );
    }
    throw new Error(`unexpected ${req.request.method} ${path}`);
  }

  async function settle(rounds = 10): Promise<void> {
    for (let i = 0; i < rounds; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  function type(selector: string, value: string): void {
    const node = el.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  async function run(): Promise<void> {
    el.querySelector<HTMLInputElement>(`input[value="${MOMENTUM.class_path}"]`)!.click();
    type('#ic-tickers', 'aapl.us msft.us');
    type('#ic-horizons', '1, 21');
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    await settle();
  }

  it('posts the strategy and tickers, then shows the IC per horizon', async () => {
    await run();
    expect(posted[0]).toMatchObject({
      strategy: { class_path: MOMENTUM.class_path },
      universe: ['AAPL.US', 'MSFT.US'],
      horizons: [1, 21],
    });
    const result = el.querySelector('app-signal-ic-result')!;
    expect(result.textContent).toContain(IC_EXPLANATION);
    const rows = [...result.querySelectorAll('tbody tr')].map((r) => r.textContent!);
    expect(rows[0]).toContain('1 bar');
    expect(rows[0]).toContain('0.05');
    expect(rows[1]).toContain('21 bars');
    expect(rows[1]).toContain('n/a');
    expect(result.textContent).toContain('0.041');
  });

  it('shows a failed result load inline with Retry', async () => {
    resultStatus = 500;
    await run();
    expect(el.textContent).toContain('Could not load the signal result');
    resultStatus = 200;
    [...el.querySelectorAll<HTMLButtonElement>('button')]
      .find((b) => b.textContent!.includes('Try again'))!
      .click();
    await settle();
    expect(el.querySelector('app-signal-ic-result')).not.toBeNull();
  });
});
