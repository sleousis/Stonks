import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { Job, MeView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { rankSweepRows } from '../../shared/lab-results/sweep-result';
import { SWEEP_RESULT } from './lab-test-fixtures';
import { SweepsPage } from './sweeps.page';

function job(patch: Partial<Job>): Job {
  return {
    id: 'sw-1',
    kind: 'lab_sweep',
    status: 'succeeded',
    progress: 1,
    created_at: '2026-09-26T10:00:00Z',
    params: {},
    ...patch,
  };
}

describe('SweepsPage', () => {
  let fixture: ComponentFixture<SweepsPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let posted: unknown[];
  let resultStatus: number;

  beforeEach(() => {
    posted = [];
    resultStatus = 200;
    TestBed.configureTestingModule({
      imports: [SweepsPage],
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
  });

  afterEach(() => controller.verify());

  async function create(me: MeView = TRADER): Promise<void> {
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(SweepsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
  }

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
    if (req.request.method === 'POST' && path === '/api/lab/sweeps') {
      posted.push(req.request.body);
      return req.flush(job({ status: 'queued', progress: 0 }));
    }
    switch (path) {
      case '/api/catalog/strategies':
        return req.flush(CATALOG);
      case '/api/catalog/intervals':
        return req.flush([{ code: '1d', is_intraday: false, seconds: 86400 }]);
      case '/api/universes':
        return req.flush(page([{ id: 'sp500', kind: 'index', name: 'S&P 500' }]));
      case '/api/jobs/sw-1':
        return req.flush(job({}));
      case '/api/lab/sweeps/sw-1/result':
        return resultStatus === 200
          ? req.flush(SWEEP_RESULT)
          : req.flush(
              { title: 'x', status: resultStatus, detail: 'Result store is down.' },
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

  it('posts the sweep, follows it and ranks the rows best first', async () => {
    await create();
    type('#sw-tickers', 'spy.us, qqq.us');
    const pick = [...el.querySelectorAll<HTMLLabelElement>('.tests label.check')].find((l) =>
      l.textContent!.includes('momentum'),
    )!;
    pick.querySelector('input')!.click();
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    await settle();

    expect(posted[0]).toMatchObject({
      universe: ['SPY.US', 'QQQ.US'],
      strategies: [MOMENTUM.class_path],
      preset: 'quick',
      budget: 20,
    });
    const names = [...el.querySelectorAll('app-sweep-result tbody tr .name')].map((n) =>
      n.textContent!.trim(),
    );
    expect(names).toEqual(['momentum', 'buy_and_hold', 'macro_regime']);
    const text = el.querySelector('app-sweep-result')!.textContent!;
    expect(text).toContain('2 of 2');
    expect(text).toContain('n/a');
    expect(text).toContain('needs an inner strategy');
  });

  it('sends a universe id when a saved universe is picked', async () => {
    await create();
    const universe = [...el.querySelectorAll<HTMLLabelElement>('.basket label')].find((l) =>
      l.textContent!.includes('saved universe'),
    )!;
    universe.querySelector('input')!.click();
    fixture.detectChanges();
    const select = el.querySelector<HTMLSelectElement>('#sw-universe')!;
    select.value = 'sp500';
    select.dispatchEvent(new Event('change'));
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    await settle();
    expect(posted[0]).toMatchObject({ universe_id: 'sp500' });
    expect((posted[0] as Record<string, unknown>)['universe']).toBeUndefined();
  });

  it('shows a failed result load inline with Retry', async () => {
    resultStatus = 500;
    await create();
    type('#sw-tickers', 'SPY.US');
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    await settle();
    expect(el.textContent).toContain('Could not load the sweep result');

    resultStatus = 200;
    const retry = [...el.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent!.includes('Try again'),
    )!;
    retry.click();
    await settle();
    expect(el.querySelector('app-sweep-result')).not.toBeNull();
  });

  it('shows a note instead of Start to someone without lab access', async () => {
    await create({ ...TRADER, role: 'viewer', scopes: ['read'] });
    const start = el.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    expect(start.disabled).toBe(true);
    expect(el.querySelector('.permission-note')?.textContent).toContain('Traders and admins only.');
  });

  it('ranks passes first, then higher scores, errors last', () => {
    const ranked = rankSweepRows(SWEEP_RESULT.rows);
    expect(ranked.map((r) => [r.rank, r.strategy, r.testsPassed, r.testsRun])).toEqual([
      [1, 'momentum', 2, 2],
      [2, 'buy_and_hold', 1, 2],
      [3, 'macro_regime', 0, 0],
    ]);
  });
});
