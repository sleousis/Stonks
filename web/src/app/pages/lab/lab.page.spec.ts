import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { tick } from '../../../testing/http';
import { BACKTEST_RESULT, CATALOG, LAB_RUN_VIEW, MOMENTUM } from '../../../testing/lab-fixtures';
import type { Job, Page } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { LabPage, canCancel, jobStrategy } from './lab.page';

function job(patch: Partial<Job>): Job {
  return {
    id: 'j1',
    kind: 'backtest',
    status: 'succeeded',
    progress: 1,
    created_at: '2026-09-26T10:00:00Z',
    params: { strategy: { class_path: MOMENTUM.class_path } },
    ...patch,
  };
}

const HISTORY: Record<string, Job[]> = {
  backtest: [job({ id: 'bt-1', created_at: '2026-09-26T09:00:00Z' })],
  lab_run: [
    job({
      id: 'lr-1',
      kind: 'lab_run',
      status: 'running',
      progress: 0.4,
      created_at: '2026-09-26T11:00:00Z',
    }),
  ],
};

describe('LabPage', () => {
  let fixture: ComponentFixture<LabPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let posted: { url: string; body: unknown }[];
  /** Status the poller sees for each job id. */
  let jobStatus: Record<string, Job>;
  let confirmed: string[];

  beforeEach(() => {
    posted = [];
    confirmed = [];
    jobStatus = {};
    TestBed.configureTestingModule({
      imports: [LabPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(new FakeChartEngine()),
        // No event stream in tests: JobsService falls back to polling GET /api/jobs/{id}.
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    const confirm = TestBed.inject(ConfirmService);
    vi.spyOn(confirm, 'confirm').mockImplementation(async (o) => {
      confirmed.push(o.title);
      return true;
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(LabPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const url = new URL(req.request.urlWithParams, 'http://localhost');
    const path = url.pathname;
    if (req.request.method === 'POST') {
      posted.push({ url: path, body: req.request.body });
      if (path === '/api/lab/backtests')
        return req.flush(job({ id: 'new-bt', status: 'queued', progress: 0 }));
      if (path === '/api/lab/runs')
        return req.flush(job({ id: 'new-lr', kind: 'lab_run', status: 'queued', progress: 0 }));
      if (path.endsWith('/cancel')) return req.flush(job({ id: 'lr-1', status: 'cancelled' }));
      throw new Error(`unexpected POST ${path}`);
    }
    switch (path) {
      case '/api/catalog/strategies':
        return req.flush(CATALOG);
      case '/api/catalog/intervals':
        return req.flush([
          { code: '1d', is_intraday: false, seconds: 86400 },
          { code: '1h', is_intraday: true, seconds: 3600 },
        ]);
      case '/api/lab/cost-models':
        return req.flush([
          { name: 'zero', description: 'No fees.', settings: {} },
          { name: 'realistic', description: 'Retail fees.', settings: {} },
        ]);
      case '/api/jobs': {
        const items = HISTORY[url.searchParams.get('kind') ?? ''] ?? [];
        const page: Page<Job> = { items, total: items.length, limit: 20, offset: 0 };
        return req.flush(page);
      }
      case '/api/lab/backtests/new-bt/result':
      case '/api/lab/backtests/bt-1/result':
        return req.flush(BACKTEST_RESULT);
      case '/api/lab/runs/new-lr/result':
        return req.flush(LAB_RUN_VIEW);
    }
    const m = /^\/api\/jobs\/([^/]+)$/.exec(path);
    if (m) return req.flush(jobStatus[m[1]] ?? job({ id: m[1] }));
    throw new Error(`unexpected GET ${path}`);
  }

  async function settle(rounds = 8): Promise<void> {
    for (let i = 0; i < rounds; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  function input(selector: string, value: string): void {
    const node = el.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  it('lists strategy classes grouped with descriptions and the recent lab jobs', async () => {
    await settle();
    const groups = [...el.querySelectorAll('#lab-panel-backtest .group-label')].map((g) =>
      g.textContent!.trim(),
    );
    expect(groups).toEqual(['Examples', 'Strategies']);
    expect(el.textContent).toContain('Buys the strongest trailing returns.');

    const rows = [...el.querySelectorAll('app-data-table tbody tr')];
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Lab run'),
      expect.stringContaining('Backtest'),
    ]);
    expect(rows[0].textContent).toContain('Momentum');
    expect(rows[0].textContent).toContain('Cancel');
    expect(rows[1].textContent).not.toContain('Cancel');
  });

  it('starts a backtest, follows it and renders the result', async () => {
    await settle();
    const radio = el.querySelector<HTMLInputElement>(
      `#lab-panel-backtest input[value="${MOMENTUM.class_path}"]`,
    )!;
    radio.click();
    fixture.detectChanges();
    expect(el.querySelector('#bt-param-lookback_days')).not.toBeNull();

    input('#bt-tickers', 'aapl.us msft.us');
    input('#bt-param-lookback_days', '30');
    jobStatus['new-bt'] = job({ id: 'new-bt', status: 'succeeded' });
    el.querySelector<HTMLButtonElement>('#lab-panel-backtest button[type="submit"]')!.click();
    await settle(12);

    expect(confirmed).toEqual(['Run a backtest of Momentum?']);
    expect(posted[0]).toMatchObject({
      url: '/api/lab/backtests',
      body: {
        strategy: {
          class_path: MOMENTUM.class_path,
          params: expect.objectContaining({ lookback_days: 30 }),
        },
        universe: ['AAPL.US', 'MSFT.US'],
        interval: '1d',
      },
    });
    const result = el.querySelector('app-backtest-result');
    expect(result?.textContent).toContain('+4.20%');
  });

  it('blocks an incomplete backtest and says what to fix', async () => {
    await settle();
    el.querySelector<HTMLButtonElement>('#lab-panel-backtest button[type="submit"]')!.click();
    await settle(2);
    expect(posted).toEqual([]);
    expect(el.textContent).toContain('Pick a strategy class.');
    expect(el.textContent).toContain('Enter at least one ticker.');
  });

  it('starts a lab run with walk-forward options and shows survival rows', async () => {
    await settle();
    el.querySelector<HTMLButtonElement>('#lab-tab-lab_run')!.click();
    fixture.detectChanges();
    el.querySelector<HTMLInputElement>(
      `#lab-panel-lab_run input[value="${MOMENTUM.class_path}"]`,
    )!.click();
    input('#lr-tickers', 'SPY.US');
    const wf = [...el.querySelectorAll<HTMLLabelElement>('#lab-panel-lab_run label.check')].find(
      (l) => l.textContent!.includes('Walk-forward'),
    )!;
    wf.querySelector('input')!.click();
    fixture.detectChanges();
    input('#lr-wf-splits', '3');
    jobStatus['new-lr'] = job({ id: 'new-lr', kind: 'lab_run', status: 'succeeded' });
    el.querySelector<HTMLButtonElement>('#lab-panel-lab_run button[type="submit"]')!.click();
    await settle(12);

    expect(posted[0]).toMatchObject({
      url: '/api/lab/runs',
      body: {
        survival_tests: ['oos', 'period_stability', 'walk_forward'],
        walk_forward: { n_splits: 3, anchored: false, metric: 'sharpe' },
        register_strategy: false,
      },
    });
    expect(el.querySelectorAll('app-lab-run-result .test').length).toBe(3);
  });

  it('opens a finished job from history and cancels a running lab run', async () => {
    await settle();
    const rows = [...el.querySelectorAll('app-data-table tbody tr')];
    rows[1].querySelector<HTMLButtonElement>('button')!.click();
    await settle();
    expect(el.querySelector('app-backtest-result')).not.toBeNull();

    const cancel = [...rows[0].querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent!.includes('Cancel'),
    )!;
    cancel.click();
    await settle();
    expect(confirmed.at(-1)).toBe('Cancel this lab run?');
    expect(posted.at(-1)?.url).toBe('/api/jobs/lr-1/cancel');
  });

  it('knows which jobs can be cancelled and names their strategy', async () => {
    await settle();
    expect(canCancel('lab_run', 'running')).toBe(true);
    expect(canCancel('backtest', 'running')).toBe(false);
    expect(canCancel('backtest', 'queued')).toBe(true);
    expect(canCancel('lab_run', 'succeeded')).toBe(false);
    expect(jobStrategy({ params: { strategy: { strategy_id: 'mom-3' } } })).toBe('mom-3');
    expect(jobStrategy({ params: {} })).toBe('–');
  });
});
