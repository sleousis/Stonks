import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { nextRequest, tick } from '../../../testing/http';
import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { LedgerRunDetail, LedgerRunView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { LedgerRunPage, paramsText, searchName } from './ledger-run.page';
import { LedgerPage, className, scoreText } from './ledger.page';

function run(i: number, over: Partial<LedgerRunView> = {}): LedgerRunView {
  return {
    id: `lab_${i}`,
    strategy_class: MOMENTUM.class_path,
    hypothesis: 'Trends persist for a few weeks.',
    premortem: 'It stops working when trends vanish.',
    tuner: 'random',
    objective: 'sharpe',
    budget: 20,
    seed: 0,
    started_at: '2026-09-25T10:00:00Z',
    finished_at: '2026-09-25T10:05:00Z',
    verdict: 'fail',
    n_trials: 20,
    n_failed: 2,
    robustness: 'did_not_survive',
    trials_ran: 18,
    trials_errored: 2,
    best_score: 0.8123,
    universe_id: null,
    tickers: 2,
    interval: '1d',
    start: '2025-10-01',
    end: '2026-04-01',
    ...over,
  };
}

describe('ledger helpers', () => {
  it('names classes, scores and parameters in plain words', () => {
    expect(className('a.b.momentum:Momentum')).toBe('Momentum');
    expect(scoreText(null)).toBe('n/a');
    expect(scoreText(0.8123)).toBe('0.81');
    expect(paramsText({ lookback_days: 20, mode: 'fast' })).toBe('Lookback days 20, mode "fast"');
    expect(paramsText({})).toBe('No settings');
    expect(searchName('random')).toBe('Random');
    expect(searchName('optuna')).toBe('Bayesian');
  });
});

describe('LedgerPage', () => {
  let fixture: ComponentFixture<LedgerPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function settle(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(LedgerPage);
    el = fixture.nativeElement;
  });

  afterEach(() => http.verify());

  it('lists runs from the server a page at a time and links each run', async () => {
    fixture.detectChanges();
    (await nextRequest(http, '/api/catalog/strategies')).flush(CATALOG);
    const req = await nextRequest(http, '/api/lab/ledger');
    expect(req.request.urlWithParams).toContain('limit=25');
    expect(req.request.urlWithParams).toContain('offset=0');
    expect(req.request.urlWithParams).not.toContain('strategy_class');
    req.flush({
      items: [run(1), run(2, { verdict: null, robustness: 'running' })],
      total: 60,
      limit: 25,
      offset: 0,
    });
    await settle();
    expect(el.textContent).toContain('60 runs');
    expect(el.textContent).toContain('Trends persist for a few weeks.');
    expect(el.textContent).toContain('0.81');
    // The run's status is its robustness, in the Lab's words.
    expect(el.textContent).toContain('Did not hold up');
    expect(el.textContent).toContain('Running');
    expect(el.querySelector('a[href="/lab/ledger/lab_1"]')).not.toBeNull();
    expect(el.textContent).toContain('more likely a good result is luck');
    expect(el.querySelector('nav[aria-label="Lab screens"] a[href="/lab/ledger"]')).not.toBeNull();
  });

  it('filters by the strategy in the URL', async () => {
    fixture.componentRef.setInput('strategy', MOMENTUM.class_path);
    fixture.detectChanges();
    (await nextRequest(http, '/api/catalog/strategies')).flush(CATALOG);
    const req = await nextRequest(http, '/api/lab/ledger');
    expect(req.request.urlWithParams).toContain('strategy_class=');
    req.flush({ items: [], total: 0, limit: 25, offset: 0 });
    await settle();
    expect(el.textContent).toContain('No runs of this strategy yet');
  });
});

describe('LedgerRunPage', () => {
  it('shows the idea, the data and every trial, with the class count explained', async () => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(LedgerRunPage);
    fixture.componentRef.setInput('runId', 'lab_1');
    fixture.detectChanges();
    const detail: LedgerRunDetail = {
      ...run(1, { universe_id: 'sp500', tickers: 500 }),
      n_trials_class: 140,
      trials: [
        {
          trial_index: 0,
          params: { lookback_days: 20 },
          score: 0.5,
          n_bars: 120,
          status: 'ok',
          outcome: 'ran',
        },
        {
          trial_index: 1,
          params: { lookback_days: 60 },
          score: null,
          n_bars: null,
          status: 'failed',
          outcome: 'error',
        },
      ],
    };
    (await nextRequest(http, '/api/lab/ledger/lab_1')).flush(detail);
    (await nextRequest(http, '/api/catalog/strategies')).flush(CATALOG);
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Momentum has had 140 trials across every run');
    expect(el.textContent).toContain('the sp500 universe (500 tickers), 2025-10-01 to 2026-04-01');
    expect(el.textContent).toContain('Lookback days 60');
    // Trials are settings that ran, and the verdict is about the robustness tests.
    const pills = [...el.querySelectorAll('tbody app-status-pill')].map((p) =>
      p.textContent!.trim(),
    );
    expect(pills).toEqual(['Done', 'Error']);
    expect(el.textContent).toContain('a run can fail with every trial done');
    expect(el.textContent).toContain('Random search, up to 20 trials');
    expect(el.textContent).toContain('It stops working when trends vanish.');
    expect(el.querySelector('a.back')?.getAttribute('href')).toBe('/lab/ledger');
    http.verify();
  });
});
