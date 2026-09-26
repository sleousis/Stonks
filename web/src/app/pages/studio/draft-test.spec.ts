import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { BacktestResult, CostModelPreset, Job, LabRunView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { makeDraft } from '../../../testing/studio-fixtures';
import { DraftTest, costsFor, parseTickers } from './draft-test';

const PRESETS: CostModelPreset[] = [
  { name: 'zero', description: 'No costs.', settings: {} },
  {
    name: 'realistic',
    description: 'Spread and fees by asset class.',
    settings: {
      default: { half_spread_bps: 2, fee_bps: 1, fee_flat: 0 },
      asset_classes: { crypto: { half_spread_bps: 5, fee_bps: 10, fee_flat: 0 } },
      impact_bps: 0,
    },
  },
];

const RESULT: BacktestResult = {
  strategy_id: 'rule',
  start: '2026-01-02',
  end: '2026-03-31',
  interval: '1d',
  equity: [
    { timestamp: '2026-01-02T00:00:00Z', value: 10_000 },
    { timestamp: '2026-01-05T00:00:00Z', value: 11_000 },
    { timestamp: '2026-01-06T00:00:00Z', value: 9_900 },
  ],
  final_return: -0.01,
  cagr: -0.04,
  sharpe: 0.35,
  max_drawdown: -0.1,
  profit_factor: 1.2,
};

const LAB: LabRunView = {
  class_path: 'stonks.strategies.rule_based:RuleStrategy',
  best_params: {},
  best_score: 0.8,
  registered_strategy_id: null,
  verdict: 'fail',
  survival_reports: [
    { test_id: 'oos', passed: true, notes: 'Holds OOS', metrics: { sharpe: 0.7 } },
    { test_id: 'period_stability', passed: false, notes: 'Unstable', metrics: {} },
  ],
};

function job(id: string, result: unknown): Job {
  return {
    id,
    kind: 'backtest',
    status: 'succeeded',
    progress: 1,
    params: {},
    created_at: '2026-09-26T10:00:00Z',
    result,
  };
}

/** A job handle that has already finished with `status`. */
function finishedHandle(jobId: string, status: 'succeeded' | 'failed' = 'succeeded'): JobHandle {
  const event = { job_id: jobId, status, progress: 1 } as const;
  return {
    jobId,
    event: signal(event),
    status: signal(status),
    progress: signal(1),
    message: signal('done'),
    error: signal(null),
    done: signal(true),
    finished: Promise.resolve(event),
    stop: () => undefined,
  };
}

describe('draft test helpers', () => {
  it('turns a cost preset into slippage and a flat fee for the asset class', () => {
    expect(costsFor(PRESETS[1], 'equity')).toEqual({ slippage_bps: 3, fee_per_trade: 0 });
    expect(costsFor(PRESETS[1], 'crypto')).toEqual({ slippage_bps: 15, fee_per_trade: 0 });
    expect(costsFor(undefined, 'equity')).toEqual({ slippage_bps: 0, fee_per_trade: 0 });
  });

  it('parses tickers typed with commas or spaces', () => {
    expect(parseTickers(' aapl.us, msft.us  AAPL.US;nvda.us')).toEqual([
      'AAPL.US',
      'MSFT.US',
      'NVDA.US',
    ]);
  });
});

const labAllowed = signal(true);

describe('DraftTest', () => {
  let fixture: ComponentFixture<DraftTest>;
  let controller: HttpTestingController;
  let chart: FakeChartEngine;
  let el: HTMLElement;
  let track: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    chart = new FakeChartEngine();
    track = vi.fn((id: string) => finishedHandle(id));
    TestBed.configureTestingModule({
      imports: [DraftTest],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeChart(chart),
        provideRouter([]),
        { provide: JobsService, useValue: { track } },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => labAllowed());
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      labAllowed() ? null : 'Traders and admins only.',
    );
    fixture = TestBed.createComponent(DraftTest);
    fixture.componentRef.setInput('draft', makeDraft());
    fixture.componentRef.setInput('specTickers', ['AAPL.US']);
    fixture.detectChanges();
    el = fixture.nativeElement;
    (await nextRequest(controller, '/api/lab/cost-models')).flush(PRESETS);
    (await nextRequest(controller, '/api/market/instruments')).flush({
      items: [],
      total: 0,
      limit: 500,
      offset: 0,
    });
    await settle();
  });

  afterEach(() => {
    labAllowed.set(true);
    controller.verify();
  });

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  function set(selector: string, value: string, event = 'change'): void {
    const input = el.querySelector(selector) as HTMLInputElement;
    input.value = value;
    input.dispatchEvent(new Event(event));
    fixture.detectChanges();
  }

  function buttonNamed(text: string): HTMLButtonElement {
    const b = [...el.querySelectorAll('button')].find((x) => x.textContent?.trim() === text);
    if (!b) throw new Error(`no button ${text}`);
    return b;
  }

  it('runs a backtest with the chosen window and cost preset, then shows results', async () => {
    const ensureSaved = vi.fn().mockResolvedValue(true);
    fixture.componentRef.setInput('ensureSaved', ensureSaved);
    set('#t-tickers', 'aapl.us msft.us', 'input');
    set('#t-start', '2026-01-01');
    set('#t-end', '2026-03-31');
    set('#t-cost', 'realistic');

    buttonNamed('Run backtest').click();
    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/backtests', 'POST');
    expect(ensureSaved).toHaveBeenCalled();
    expect(req.request.body).toEqual({
      universe: ['AAPL.US', 'MSFT.US'],
      start: '2026-01-01',
      end: '2026-03-31',
      interval: '1d',
      initial_cash: 10000,
      rebalance_every_bars: 1,
      slippage_bps: 3,
      fee_per_trade: 0,
    });
    req.flush({ ...job('job_bt', null), status: 'queued', progress: 0 });
    (await nextRequest(controller, '/api/jobs/job_bt')).flush(job('job_bt', RESULT));
    await settle();

    expect(track).toHaveBeenCalledWith('job_bt', expect.anything());
    expect(el.querySelector('app-backtest-result')).not.toBeNull();
    const text = el.textContent ?? '';
    expect(text).toContain('-1.00%');
    expect(text).toContain('0.35');
    expect(text).toContain('-10.00%');
    const series = chart.last ?? [];
    expect(series.map((s) => s.id)).toEqual(['equity', 'drawdown']);
    expect(series[0].points[0]).toEqual({ time: '2026-01-02', value: 10_000 });
    expect(series[1].points[2].value).toBeCloseTo(-0.1);
  });

  it('refuses to run without tickers or with an inverted window', async () => {
    set('#t-tickers', '', 'input');
    set('#t-start', '2026-05-01');
    set('#t-end', '2026-01-01');
    buttonNamed('Run backtest').click();
    await settle();
    expect(el.textContent).toContain('Enter at least one ticker');
    expect(el.textContent).toContain('start date must be before the end date');
  });

  it('runs a lab run and shows survival verdicts as pass / fail pills', async () => {
    buttonNamed('Run lab').click();
    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/lab-runs', 'POST');
    expect(req.request.body).toMatchObject({
      universe: ['AAPL.US'],
      objective: 'sharpe',
      survival_tests: ['oos', 'period_stability'],
    });
    req.flush({ ...job('job_lab', null), status: 'queued', progress: 0 });
    (await nextRequest(controller, '/api/jobs/job_lab')).flush(job('job_lab', LAB));
    await settle();

    const text = el.textContent ?? '';
    expect(el.querySelector('app-lab-run-result')).not.toBeNull();
    expect(text).toContain('Failed');
    const pills = [...el.querySelectorAll('app-lab-run-result .test app-status-pill')].map((p) =>
      p.textContent?.trim(),
    );
    expect(pills).toEqual(['pass', 'fail']);
    expect(text).toContain('Out of sample');
    expect(text).toContain('Unstable');
  });

  it('disables backtests and lab runs with a reason without lab.run (UI-06)', () => {
    labAllowed.set(false);
    fixture.detectChanges();
    expect(buttonNamed('Run backtest').disabled).toBe(true);
    expect(buttonNamed('Run lab').disabled).toBe(true);
    expect(el.querySelectorAll('app-permission-note p')).toHaveLength(2);
    expect(el.textContent).toContain('Traders and admins only.');
  });
});
