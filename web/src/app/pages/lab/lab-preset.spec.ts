import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { tick } from '../../../testing/http';
import { STRATEGY_METADATA } from '../../../testing/strategy-fixtures';
import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { StrategyDetail } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { LabPage } from './lab.page';

function registered(id: string, classPath: string): StrategyDetail {
  return {
    id,
    class_path: classPath,
    status: 'shadow',
    params: { lookback_days: 60, mode: 'slow' },
    applicable_asset_classes: ['equity'],
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
    metadata: STRATEGY_METADATA,
    status_history: [],
    survival_reports: [],
  };
}

/** `/lab?strategy=<id>`: the forms start from a registered strategy. */
describe('LabPage with ?strategy=', () => {
  let fixture: ComponentFixture<LabPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let strategies: Record<string, StrategyDetail>;

  beforeEach(() => {
    strategies = {
      'momentum-v3': registered('momentum-v3', MOMENTUM.class_path),
      'custom-v1': registered('custom-v1', 'user_strategies.custom:Custom'),
    };
    TestBed.configureTestingModule({
      imports: [LabPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(new FakeChartEngine()),
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(LabPage);
    el = fixture.nativeElement;
  });

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
    if (path === '/api/catalog/strategies') return req.flush(CATALOG);
    if (path === '/api/catalog/intervals')
      return req.flush([{ code: '1d', is_intraday: false, seconds: 86400 }]);
    if (path === '/api/lab/cost-models') return req.flush([]);
    if (path === '/api/jobs') return req.flush({ items: [], total: 0, limit: 20, offset: 0 });
    if (path === '/api/lab/survival-tests') return req.flush([]);
    if (path === '/api/lab/survival-presets') return req.flush([]);
    if (path === '/api/universes') return req.flush({ items: [], total: 0, limit: 200, offset: 0 });
    const m = /^\/api\/strategies\/([^/]+)$/.exec(path);
    if (m) {
      const s = strategies[decodeURIComponent(m[1])];
      return s
        ? req.flush(s)
        : req.flush(
            { title: 'Not found', status: 404, detail: 'no strategy' },
            { status: 404, statusText: 'Not Found' },
          );
    }
    throw new Error(`unexpected ${req.request.method} ${path}`);
  }

  async function open(strategy: string): Promise<void> {
    fixture.componentRef.setInput('strategy', strategy);
    fixture.detectChanges();
    for (let i = 0; i < 8; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('preselects the registered class and parameters in both forms', async () => {
    await open('momentum-v3');

    expect(el.querySelector('.preset')?.textContent).toContain('momentum-v3');
    const bt = el.querySelector<HTMLInputElement>(
      `#lab-panel-backtest input[value="${MOMENTUM.class_path}"]`,
    );
    expect(bt?.checked).toBe(true);
    expect(el.querySelector<HTMLInputElement>('#bt-param-lookback_days')?.value).toBe('60');
    const lr = el.querySelector<HTMLInputElement>(
      `#lab-panel-lab_run input[value="${MOMENTUM.class_path}"]`,
    );
    expect(lr?.checked).toBe(true);
  });

  it('says when the registered class is not in the catalog', async () => {
    await open('custom-v1');
    expect(el.querySelector('.preset')?.textContent).toContain('not available to test here');
    expect(el.querySelector('#bt-param-lookback_days')).toBeNull();
  });

  it('says when the strategy id is unknown', async () => {
    await open('ghost');
    expect(el.querySelector('.preset')?.textContent).toContain('Could not load that strategy');
  });
});
