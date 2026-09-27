import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TRADER } from '../../../../testing/auth-fixtures';
import { provideFakeChart } from '../../../../testing/fake-chart';
import { nextRequest, page, tick } from '../../../../testing/http';
import { CATALOG, LAB_RUN_VIEW } from '../../../../testing/lab-fixtures';
import type { Job, StrategyClassInfo, UniverseView, WatchlistView } from '../../../api/models';
import { provideApi } from '../../../api/provide-api';
import { SessionService } from '../../../core/auth/session.service';
import { ConfirmService } from '../../../core/confirm/confirm.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../../core/jobs/jobs.service';
import { FactorDetailPage } from './factor-detail.page';
import { MOM, TEARSHEET } from './factor-test-fixtures';

const FACTOR_CLASS: StrategyClassInfo = {
  class_path: 'stonks.strategies.examples.factor_strategy:FactorStrategy',
  name: 'factor',
  source: 'builtin',
  description: 'Holds the top slice by a factor.',
  applicable_asset_classes: ['equity'],
  parameters: [],
};
const UNIVERSE: UniverseView = { id: 'sp40', kind: 'list', name: 'Forty names', spec: {} };
const WATCHLIST: WatchlistView = {
  id: 'wl1',
  name: 'Tech',
  tickers: ['AAA.US', 'BBB.US'],
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
};

function job(id: string, kind: string, patch: Partial<Job> = {}): Job {
  return {
    id,
    kind,
    status: 'succeeded',
    progress: 1,
    created_at: '2026-09-27T10:00:00Z',
    params: {},
    ...patch,
  };
}

describe('FactorDetailPage', () => {
  let fixture: ComponentFixture<FactorDetailPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let posted: Record<string, unknown>;

  async function open(factorId: string, expression?: string): Promise<void> {
    posted = {};
    TestBed.configureTestingModule({
      imports: [FactorDetailPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(),
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    controller = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
    fixture = TestBed.createComponent(FactorDetailPage);
    fixture.componentRef.setInput('factorId', factorId);
    if (expression) fixture.componentRef.setInput('expression', expression);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
  }

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
    if (req.request.method === 'POST') {
      posted[path] = req.request.body;
      switch (path) {
        case '/api/factors/check':
          return req.flush({ ok: true, canonical: '$close / Ref($close, 5)', lookback_bars: 5 });
        case '/api/factors/values':
          return req.flush({
            factor_id: 'mom_12_1',
            as_of: '2026-09-25',
            direction: 1,
            values: [
              { ticker: 'AAA.US', value: 0.31, rank: 1 },
              { ticker: 'BBB.US', value: -0.02, rank: 2 },
            ],
            missing: ['CCC.US'],
          });
        case '/api/factors/tearsheets':
          return req.flush(job('ts-1', 'factor_tearsheet', { status: 'queued', progress: 0 }));
        case '/api/lab/runs':
          return req.flush(job('lr-1', 'lab_run', { status: 'queued', progress: 0 }));
      }
    }
    switch (path) {
      case '/api/factors/mom_12_1':
        return req.flush(MOM);
      case '/api/universes':
        return req.flush(page([UNIVERSE]));
      case '/api/watchlists':
        return req.flush(page([WATCHLIST]));
      case '/api/catalog/strategies':
        return req.flush([...CATALOG, FACTOR_CLASS]);
      case '/api/jobs/ts-1':
        return req.flush(job('ts-1', 'factor_tearsheet'));
      case '/api/factors/tearsheets/ts-1/result':
        return req.flush(TEARSHEET);
      case '/api/jobs/lr-1':
        return req.flush(job('lr-1', 'lab_run'));
      case '/api/lab/runs/lr-1/result':
        return req.flush(LAB_RUN_VIEW);
    }
    throw new Error(`unexpected ${req.request.method} ${path}`);
  }

  async function settle(rounds = 12): Promise<void> {
    for (let i = 0; i < rounds; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  function panel(title: string): HTMLElement {
    const h = [...el.querySelectorAll('h2')].find((x) => x.textContent!.trim() === title);
    if (!h) throw new Error(`no panel ${title}`);
    return h.closest('section')!;
  }

  function pick(root: HTMLElement, selector: string, value: string): void {
    const select = root.querySelector<HTMLSelectElement>(selector)!;
    select.value = value;
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
  }

  function submit(root: HTMLElement): void {
    root.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
  }

  it('shows what the factor measures and links its formula to the editor', async () => {
    await open('mom_12_1');
    const facts = panel('mom_12_1');
    expect(facts.textContent).toContain(MOM.hypothesis);
    expect(facts.textContent).toContain('Higher is better');
    expect(facts.textContent).toContain('252 bars');
    const edit = facts.querySelector<HTMLAnchorElement>('a[href^="/lab/factors/formula"]')!;
    expect(decodeURIComponent(edit.getAttribute('href')!)).toContain(MOM.expression!);
  });

  it('shows the values on a date for a universe', async () => {
    await open('mom_12_1');
    const values = panel('Values on a date');
    pick(values, '#fv-universe', 'sp40');
    const date = values.querySelector<HTMLInputElement>('#fv-date')!;
    date.value = '2026-09-25';
    date.dispatchEvent(new Event('input'));
    submit(values);
    await settle();
    expect(posted['/api/factors/values']).toEqual({
      factor: 'mom_12_1',
      as_of: '2026-09-25',
      universe_id: 'sp40',
    });
    const rows = [...values.querySelectorAll('tbody tr')].map((r) => r.textContent!);
    expect(rows[0]).toContain('AAA.US');
    expect(values.textContent).toContain('No value for 1 name: CCC.US');
  });

  it('runs a tear sheet on a watchlist and shows the result', async () => {
    await open('mom_12_1');
    const sheet = panel('Tear sheet');
    sheet.querySelector<HTMLInputElement>('input[name="ts-basket"][value="watchlist"]')!.click();
    fixture.detectChanges();
    pick(sheet, '#ts-watchlist', 'wl1');
    submit(sheet);
    await settle();
    expect(posted['/api/factors/tearsheets']).toMatchObject({
      factor: 'mom_12_1',
      universe: ['AAA.US', 'BBB.US'],
      horizons: [1, 5, 21],
      n_quantiles: 5,
    });
    expect(sheet.querySelector('app-factor-tearsheet-result')).not.toBeNull();
    expect(sheet.textContent).toContain('IC per horizon');
  });

  it('starts a factor strategy lab run with the factor pinned', async () => {
    await open('mom_12_1');
    const run = panel('Test as a strategy');
    pick(run, '#fr-universe', 'sp40');
    submit(run);
    await settle();
    expect(posted['/api/lab/runs']).toMatchObject({
      strategy: { class_path: FACTOR_CLASS.class_path, params: { factor: 'mom_12_1' } },
      universe_id: 'sp40',
      preset: 'quick',
      hypothesis: MOM.hypothesis,
    });
    expect(run.querySelector('app-lab-run-result')).not.toBeNull();
  });

  it('asks for a hypothesis and a basket before a run', async () => {
    await open('mom_12_1');
    const run = panel('Test as a strategy');
    const text = run.querySelector<HTMLTextAreaElement>('#fr-hypothesis')!;
    text.value = '';
    text.dispatchEvent(new Event('input'));
    submit(run);
    await settle();
    expect(posted['/api/lab/runs']).toBeUndefined();
    expect(run.textContent).toContain('Pick a universe.');
    expect(run.textContent).toContain('Say why this factor should rank future returns');
  });

  it('opens the tools on a formula once it checks out', async () => {
    await open('formula', '$close/Ref($close,5)');
    await settle();
    expect(posted['/api/factors/check']).toEqual({ expression: '$close/Ref($close,5)' });
    expect(panel('Values on a date')).toBeTruthy();
    const run = panel('Test as a strategy');
    expect(run.querySelector<HTMLTextAreaElement>('#fr-hypothesis')!.value).toBe('');
    expect(run.textContent).toContain('A formula has no reason of its own');
  });
});
