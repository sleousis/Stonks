import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { BACKTEST_RESULT, CATALOG, LAB_RUN_VIEW, MOMENTUM } from '../../../testing/lab-fixtures';
import type { Job, MeView, Page } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { LabPage, canCancel, jobStrategy, kindLabel } from './lab.page';
import { SURVIVAL_TEST_CATALOG, SWEEP_RESULT } from './lab-test-fixtures';

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
  lab_sweep: [
    job({
      id: 'sw-1',
      kind: 'lab_sweep',
      created_at: '2026-09-26T08:00:00Z',
      params: { start: '2025-09-26', end: '2026-09-26', universe: ['SPY.US'] },
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

  let created = false;

  async function create(me: MeView = TRADER): Promise<void> {
    created = true;
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(LabPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  beforeEach(async () => {
    created = false;
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
      case '/api/lab/survival-tests':
        return req.flush(SURVIVAL_TEST_CATALOG);
      case '/api/lab/survival-presets':
        return req.flush([]);
      case '/api/universes':
        return req.flush({ items: [], total: 0, limit: 200, offset: 0 });
      case '/api/lab/sweeps/sw-1/result':
        return req.flush(SWEEP_RESULT);
    }
    const m = /^\/api\/jobs\/([^/]+)$/.exec(path);
    if (m) return req.flush(jobStatus[m[1]] ?? job({ id: m[1] }));
    throw new Error(`unexpected GET ${path}`);
  }

  async function settle(rounds = 8): Promise<void> {
    if (!created) await create();
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
      expect.stringContaining('Sweep'),
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

  function pickSuite(label: string): void {
    const suite = [...el.querySelectorAll<HTMLLabelElement>('#lab-panel-lab_run label.suite')].find(
      (l) => l.querySelector('.suite-name')!.textContent!.trim() === label,
    )!;
    suite.querySelector('input')!.click();
    fixture.detectChanges();
  }

  async function openLabRunForm(): Promise<void> {
    await settle();
    el.querySelector<HTMLButtonElement>('#lab-tab-lab_run')!.click();
    fixture.detectChanges();
    el.querySelector<HTMLInputElement>(
      `#lab-panel-lab_run input[value="${MOMENTUM.class_path}"]`,
    )!.click();
    input('#lr-tickers', 'SPY.US');
  }

  function submitLabRun(): void {
    el.querySelector<HTMLButtonElement>('#lab-panel-lab_run button[type="submit"]')!.click();
    fixture.detectChanges();
  }

  it('starts a standard-preset run with walk-forward and advanced test options', async () => {
    await openLabRunForm();
    pickSuite('Standard');
    expect(el.querySelector('#lab-panel-lab_run .suite.chosen')?.textContent).toContain(
      'walk-forward',
    );
    input('#lr-wf-splits', '3');

    // An out-of-range option blocks the run and shows its error next to the field.
    input('#lr-opt-deflated_sharpe-min_dsr', '0.5');
    submitLabRun();
    await settle(2);
    expect(posted).toEqual([]);
    const details = el.querySelector<HTMLDetailsElement>('#lab-panel-lab_run details.advanced')!;
    expect(details.open).toBe(true);
    expect(el.querySelector('#lr-opt-deflated_sharpe-min_dsr-hint')?.textContent).toContain(
      'Must be at least 0.8 and at most 0.99.',
    );

    input('#lr-opt-deflated_sharpe-min_dsr', '0.9');
    jobStatus['new-lr'] = job({ id: 'new-lr', kind: 'lab_run', status: 'succeeded' });
    submitLabRun();
    await settle(12);

    expect(posted[0]).toMatchObject({
      url: '/api/lab/runs',
      body: {
        preset: 'standard',
        walk_forward: { n_splits: 3, metric: 'sharpe' },
        test_options: { deflated_sharpe: { min_dsr: 0.9 } },
      },
    });
    const body = posted[0].body as Record<string, unknown>;
    expect(body['survival_tests']).toBeUndefined();
    expect(body['register_strategy']).toBeUndefined();
    expect(el.querySelectorAll('app-lab-run-result .test').length).toBe(3);
  });

  it('registers only if the run passes, and asks for a hypothesis first', async () => {
    await openLabRunForm();
    const register = [...el.querySelectorAll<HTMLLabelElement>('#lab-panel-lab_run label.check')]
      .find((l) => l.textContent!.includes('Register the fitted strategy'))!
      .querySelector('input')!;
    register.click();
    fixture.detectChanges();
    // Registering switches the quick suite to promotion, as the API does.
    expect(el.querySelector('#lab-panel-lab_run .suite.chosen .suite-name')?.textContent).toContain(
      'Promotion',
    );

    submitLabRun();
    await settle(2);
    expect(posted).toEqual([]);
    expect(el.textContent).toContain('Say why it should make money before registering it.');

    const hypothesis = el.querySelector<HTMLTextAreaElement>('#lr-hypothesis')!;
    hypothesis.value = 'Slow money chases recent winners for months.';
    hypothesis.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    jobStatus['new-lr'] = job({ id: 'new-lr', kind: 'lab_run', status: 'succeeded' });
    submitLabRun();
    await settle(12);

    expect(confirmed).toEqual(['Start a lab run of Momentum?']);
    expect(posted[0].body).toMatchObject({
      preset: 'promotion',
      register_if_passes: true,
      hypothesis: 'Slow money chases recent winners for months.',
    });
    expect((posted[0].body as Record<string, unknown>)['register_strategy']).toBeUndefined();
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

  it('opens a sweep from history and shows its rows best first', async () => {
    await settle();
    const row = [...el.querySelectorAll('app-data-table tbody tr')].find((r) =>
      r.textContent!.includes('Sweep'),
    )!;
    expect(row.textContent).toContain('All strategies');
    row.querySelector<HTMLButtonElement>('button')!.click();
    await settle();
    const names = [...el.querySelectorAll('app-sweep-result tbody tr .name')].map((n) =>
      n.textContent!.trim(),
    );
    expect(names).toEqual(['momentum', 'buy_and_hold', 'macro_regime']);
    expect(el.querySelector('app-sweep-result')!.textContent).toContain('n/a');
  });

  it('shows a note instead of Run to someone without lab access', async () => {
    await create({ ...TRADER, role: 'viewer', scopes: ['read'] });
    await settle();
    const run = el.querySelector<HTMLButtonElement>('#lab-panel-backtest button[type="submit"]')!;
    expect(run.disabled).toBe(true);
    expect(el.querySelector('#lab-panel-backtest .permission-note')?.textContent).toContain(
      'Traders and admins only.',
    );
    const rows = [...el.querySelectorAll('app-data-table tbody tr')];
    expect(rows.some((r) => r.textContent!.includes('Cancel'))).toBe(false);
  });

  it('knows which jobs can be cancelled and names their strategy', async () => {
    await settle();
    expect(canCancel('lab_run', 'running')).toBe(true);
    expect(canCancel('backtest', 'running')).toBe(false);
    expect(canCancel('backtest', 'queued')).toBe(true);
    expect(canCancel('lab_run', 'succeeded')).toBe(false);
    expect(jobStrategy({ params: { strategy: { strategy_id: 'mom-3' } } })).toBe('mom-3');
    expect(jobStrategy({ params: {} })).toBe('–');
    expect(jobStrategy({ params: { start: 'x', strategies: ['a:B', 'c:D'] } })).toBe(
      '2 strategies',
    );
    expect(kindLabel('signal_ic')).toBe('Signal IC');
  });
});
