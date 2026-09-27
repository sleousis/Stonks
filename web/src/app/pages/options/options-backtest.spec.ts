import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TRADER } from '../../../testing/auth-fixtures';
import { provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import type { MeView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { OptionsBacktest } from './options-backtest';
import { BACKTEST, STRATEGIES, UNDERLYING, job } from './options-test-fixtures';

const VIEWER: MeView = { ...TRADER, role: 'viewer', scopes: ['read'] };

describe('OptionsBacktest', () => {
  let fixture: ComponentFixture<OptionsBacktest>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  async function render(me: MeView): Promise<void> {
    TestBed.configureTestingModule({
      imports: [OptionsBacktest],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(),
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(OptionsBacktest);
    fixture.componentRef.setInput('strategies', STRATEGIES);
    fixture.componentRef.setInput('underlyings', [UNDERLYING]);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await tick(5);
    fixture.detectChanges();
  }

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
    if (req.request.method === 'POST' && path === '/api/options/backtests') {
      return req.flush(job('ob-2', { status: 'queued', progress: 0 }));
    }
    if (path === '/api/jobs/ob-2') return req.flush(job('ob-2'));
    if (path === '/api/options/backtests/ob-2/result') {
      return req.flush({ ...BACKTEST, synthetic: false, verdict: 'passed', validation: [] });
    }
    throw new Error(`unexpected ${req.request.method} ${path}`);
  }

  async function settle(rounds = 20): Promise<void> {
    for (let i = 0; i < rounds; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('leaves the run button off without the lab permission', async () => {
    await render(VIEWER);
    const run = [...el.querySelectorAll('button')].find(
      (b) => b.textContent!.trim() === 'Run backtest',
    )!;
    expect(run.disabled).toBe(true);
    expect(el.querySelector('app-permission-note')).not.toBeNull();
  });

  it('shows a passing result on market chains without the warning', async () => {
    await render(TRADER);
    const select = el.querySelector<HTMLSelectElement>('#obt-strategy')!;
    select.value = 'vertical_spread';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(el.textContent).toContain('This strategy has no parameters.');
    const run = [...el.querySelectorAll('button')].find(
      (b) => b.textContent!.trim() === 'Run backtest',
    )!;
    run.click();
    await settle();
    expect(el.textContent).toContain('Checks passed');
    expect(el.textContent).not.toContain('never evidence');
    expect(el.querySelector('app-time-series-chart')).not.toBeNull();
    expect(el.querySelector('.checks')).toBeNull();
  });
});
