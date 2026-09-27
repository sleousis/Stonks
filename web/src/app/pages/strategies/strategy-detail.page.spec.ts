import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, input, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type {
  BrokerInfo,
  OrderView,
  PnlRowView,
  StatusChangeView,
  StrategyDetail,
} from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ToastService } from '../../core/notify/toast.service';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import {
  answerDialog,
  dialogForm,
  fillDialog,
  goLiveReport,
  isHoldDialog,
} from '../../../testing/status-dialog';
import { STRATEGY_METADATA } from '../../../testing/strategy-fixtures';
import { LIFECYCLE } from '../../shared/governance-labels';
import { FollowPanel } from './follow-panel';
import { StrategyDetailPage, historyEntries, paperPerformance } from './strategy-detail.page';

/** The follow panel has its own spec; here only where it shows matters. */
@Component({ selector: 'app-follow-panel', template: 'Follow panel' })
class FollowPanelStub {
  readonly strategyId = input.required<string>();
}

const DETAIL: StrategyDetail = {
  id: 'momentum-v3',
  status: 'shadow',
  class_path: 'stonks.strategies.momentum.MomentumStrategy',
  applicable_asset_classes: ['equity', 'crypto'],
  params: { lookback: 126, top_n: 5, universe: ['AAPL.US', 'MSFT.US'] },
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-20T10:00:00Z',
  metadata: STRATEGY_METADATA,
  status_history: [],
  survival_reports: [
    { test_id: 'oos', passed: true, notes: 'held up out of sample', metrics: { cagr_oos: 0.1234 } },
    { test_id: 'drift', passed: false, notes: '', metrics: { drift: 0.31, limit: null } },
  ],
};

const HISTORY: StatusChangeView[] = [
  {
    id: 1,
    kind: 'status',
    from_status: null,
    to_status: 'shadow',
    actor: 'lab',
    reason: 'registered by lab run',
    override: false,
    golive_passed: null,
    golive_report: null,
    created_at: '2026-09-01T10:00:00Z',
  },
  {
    id: 2,
    kind: 'status',
    from_status: 'shadow',
    to_status: 'active',
    actor: 'api',
    reason: 'paper period looked fine but was short',
    override: true,
    golive_passed: false,
    golive_report: null,
    created_at: '2026-09-10T10:00:00Z',
  },
];

const PNL: PnlRowView[] = [
  {
    day: '2026-09-01',
    total_value: 10000,
    cumulative_return: 0,
    drawdown: 0,
    daily_change: null,
    daily_return: null,
    days_elapsed: 0,
  },
  {
    day: '2026-09-02',
    total_value: 9500,
    cumulative_return: -0.05,
    drawdown: -0.05,
    daily_change: -500,
    daily_return: -0.05,
    days_elapsed: 1,
  },
  {
    day: '2026-09-22',
    total_value: 10800,
    cumulative_return: 0.08,
    drawdown: 0,
    daily_change: 100,
    daily_return: 0.01,
    days_elapsed: 21,
  },
];

const ORDER: OrderView = {
  client_id: 'c1',
  tick_id: 't1',
  strategy_id: 'momentum-v3',
  ticker: 'AAPL.US',
  side: 'buy',
  quantity: 12,
  order_type: 'market',
  limit_price: null,
  status: 'filled',
  status_reason: null,
  broker_order_id: null,
  created_at: '2026-09-20T14:00:00Z',
  updated_at: '2026-09-20T14:00:00Z',
};

const SIMULATED: BrokerInfo = {
  kind: 'simulated',
  paper: true,
  allow_live: false,
  credentials_configured: false,
};
const REAL: BrokerInfo = {
  kind: 'alpaca',
  paper: false,
  allow_live: true,
  credentials_configured: true,
};

const page = <T>(items: T[]) => ({ items, total: items.length, limit: 10, offset: 0 });

describe('StrategyDetailPage', () => {
  let fixture: ComponentFixture<StrategyDetailPage>;
  let controller: HttpTestingController;
  let toasts: ToastService;
  let el: HTMLElement;
  let chart: FakeChartEngine;
  const allowed = signal(true);

  beforeEach(() => {
    allowed.set(true);
    chart = new FakeChartEngine();
    TestBed.configureTestingModule({
      imports: [StrategyDetailPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(chart),
      ],
    });
    TestBed.overrideComponent(StrategyDetailPage, {
      remove: { imports: [FollowPanel] },
      add: { imports: [FollowPanelStub] },
    });
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed());
    vi.spyOn(session, 'whyNot').mockImplementation(() => (allowed() ? null : 'Admins only.'));
    controller = TestBed.inject(HttpTestingController);
    toasts = TestBed.inject(ToastService);
    fixture = TestBed.createComponent(StrategyDetailPage);
    fixture.componentRef.setInput('id', 'momentum-v3');
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  /** Flush the page's reads; a paper strategy also loads its go-live verdict and value. */
  async function load(
    detail = DETAIL,
    history = HISTORY,
    opts: {
      passed?: boolean;
      pnl?: PnlRowView[];
      orders?: OrderView[];
      broker?: BrokerInfo;
    } = {},
  ): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(detail);
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page(history));
    (await nextRequest(controller, '/api/orders')).flush(page(opts.orders ?? []));
    await settle();
    if (detail.status === 'active') {
      (await nextRequest(controller, '/api/brokers')).flush(opts.broker ?? SIMULATED);
      await settle();
    }
    if (detail.status === 'shadow') {
      (await nextRequest(controller, '/api/strategies/momentum-v3/golive')).flush(
        goLiveReport('momentum-v3', opts.passed ?? false),
      );
      (await nextRequest(controller, '/api/shadow/strategies/momentum-v3/pnl')).flush({
        strategy_id: 'momentum-v3',
        rows: opts.pnl ?? PNL,
      });
      await settle();
    }
  }

  async function reloaded(status: StrategyDetail['status']): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush({ ...DETAIL, status });
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page(HISTORY));
    await settle();
    if (status === 'active') {
      (await nextRequest(controller, '/api/brokers')).flush(SIMULATED);
      await settle();
    }
  }

  /** The reads behind the go-live ticket: gate, broker and followers. */
  async function ticketReads(passed: boolean, broker: BrokerInfo = SIMULATED): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3/golive')).flush(
      goLiveReport('momentum-v3', passed),
    );
    (await nextRequest(controller, '/api/brokers')).flush(broker);
    (await nextRequest(controller, '/api/subscriptions')).flush(page([]));
    await settle();
  }

  function button(label: string): HTMLButtonElement | undefined {
    return Array.from(el.querySelectorAll<HTMLButtonElement>('app-page-header button')).find(
      (b) => b.textContent?.trim() === label,
    );
  }

  it('renders kind, params, research card and survival reports', async () => {
    await load();
    const text = el.textContent ?? '';

    expect(el.querySelector('h1')?.textContent).toContain('momentum-v3');
    // A name, not the Python class path, under the title.
    const subtitle = el.querySelector('app-page-header p')?.textContent ?? '';
    expect(subtitle).toContain('Momentum strategy. Paper trading.');
    expect(subtitle).not.toContain('stonks.');
    // The class path is secondary, inside a details block.
    expect(el.querySelector('details.tech')?.textContent).toContain(
      'stonks.strategies.momentum.MomentumStrategy',
    );
    expect(text).toContain('equity');
    expect(text).toContain('2026-09-01');
    // Parameter names read as words, not keys (UX-68).
    expect(text).toContain('Lookback');
    expect(text).toContain('Top n');
    expect(text).toContain('["AAPL.US","MSFT.US"]');

    const research = el.querySelector('section[aria-labelledby="research-title"]')!;
    expect(research.querySelector('blockquote')?.textContent).toContain(
      'Recent winners keep winning',
    );
    expect(research.textContent).toContain('Trend');
    expect(research.textContent).toContain('21 bars');

    const reports = el.querySelectorAll('.report');
    expect(reports.length).toBe(2);
    // Survival test headings use labels, not ids like "oos".
    const headings = [...el.querySelectorAll('.report h3')].map((h) => h.textContent?.trim());
    expect(headings[0]).toContain('Out of sample');
    expect(headings[1]).toContain('Drift');
    expect(headings.join(' ')).not.toMatch(/\boos\b/);
    expect(reports[0].textContent).toContain('CAGR (held out)');
    expect(reports[0].textContent).toContain('12.34%');
    expect(reports[1].textContent).toContain('n/a');
    expect(reports[1].classList).toContain('failed');
    expect(text).toContain('1 of 2 passed');

    expect(el.querySelector('a[href="/lab?strategy=momentum-v3"]')).not.toBeNull();
    expect(el.querySelector('a[href="/go-live?strategy=momentum-v3"]')).not.toBeNull();
  });

  it('uses trader words only: no system words outside what people typed (UX-09)', async () => {
    await load();
    const copy = el.cloneNode(true) as HTMLElement;
    // Reasons are what people wrote; the page's own words must be clean.
    copy.querySelectorAll('.timeline .reason').forEach((r) => r.remove());
    expect(copy.textContent).not.toMatch(/shadow|promot|regist|retire/i);
    const pills = [...el.querySelectorAll('.timeline app-status-pill')].map((p) =>
      p.textContent?.trim(),
    );
    expect(pills).toContain('Paper trading');
    expect(pills).toContain('Live');
  });

  it('shows the status history newest first, with reasons and overrides', async () => {
    await load();
    const items = el.querySelectorAll('.timeline li');
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain('paper period looked fine but was short');
    expect(items[0].textContent).toContain('Gate overridden');
    expect(items[0].textContent).toContain('Go-live failed');
    expect(items[0].classList).toContain('override');
    expect(items[1].textContent).toContain('registered by lab run');
    expect(historyEntries(HISTORY).map((h) => h.id)).toEqual([2, 1]);
  });

  it('keeps the page when only the history fails', async () => {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush({
      ...DETAIL,
      status: 'retired',
    });
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(
      { title: 'Server error', status: 500, detail: 'boom' },
      { status: 500, statusText: 'Server Error' },
    );
    (await nextRequest(controller, '/api/orders')).flush(page([]));
    await settle();
    expect(el.querySelector('app-follow-panel')).toBeNull();
    expect(el.textContent).toContain('Could not load the status history');
    expect(el.querySelectorAll('.report').length).toBe(2);
  });

  it('offers to follow a paper or live strategy, not a stopped one', async () => {
    await load();
    expect(el.querySelector('app-follow-panel')).not.toBeNull();
  });

  it('offers Go live only at the Ready stage (UX-23)', async () => {
    await load(DETAIL, HISTORY, { passed: true });
    expect(button(LIFECYCLE.live.label)!.classList).toContain('btn-primary');
    expect(button(LIFECYCLE.stop.label)).toBeDefined();
    expect(button(LIFECYCLE.pause.label)).toBeUndefined();
    expect(button('Override…')).toBeUndefined();
  });

  it('failing paper strategy shows no primary Go live, admins get Override (UX-23)', async () => {
    await load();
    expect(button(LIFECYCLE.live.label)).toBeUndefined();
    const override = button('Override…')!;
    expect(override.classList).not.toContain('btn-primary');
    override.click();
    await ticketReads(false);
    const form = dialogForm(el)!;
    expect(form.textContent).toContain('without passing the check');
    expect(form.querySelector('.ticket')).not.toBeNull();
    answerDialog(fixture, { reason: 'Owner accepts a short paper period', typed: 'override' });
    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    expect(post.request.body).toEqual({
      reason: 'Owner accepts a short paper period',
      override: true,
    });
    post.flush({ ...DETAIL, status: 'active' });
    await reloaded('active');
  });

  it('stopped strategy offers no Go live, only Start paper trading (UX-23)', async () => {
    await load({ ...DETAIL, status: 'retired' });
    const labels = [...el.querySelectorAll('app-page-header button')].map((b) =>
      b.textContent?.trim(),
    );
    expect(labels).toEqual([LIFECYCLE.paper.label]);
  });

  it('shows the display name in the header and the id under Technical details (UX-27)', async () => {
    await load({ ...DETAIL, id: 'momentum_3fa9c21b', status: 'retired' });
    expect(el.querySelector('h1')!.textContent).toBe('Momentum 3fa9');
    expect(el.querySelector('details.tech')!.textContent).toContain('momentum_3fa9c21b');
  });

  it('shows the LIVE stamp by the title of a live strategy on a real broker (UX-09)', async () => {
    await load({ ...DETAIL, status: 'active' }, HISTORY, { broker: REAL });
    expect(el.querySelector('app-page-header app-mode-stamp')!.textContent).toContain('LIVE');
  });

  it('shows no stamp for a live strategy on a paper broker', async () => {
    await load({ ...DETAIL, status: 'active' });
    expect(el.querySelector('app-page-header app-mode-stamp')).toBeNull();
  });

  it('goes live with a reason and a hold after showing the go-live result', async () => {
    await load(DETAIL, HISTORY, { passed: true });
    const success = vi.spyOn(toasts, 'success');

    button(LIFECYCLE.live.label)!.click();
    await ticketReads(true);

    const form = dialogForm(el)!;
    // An order ticket with a PAPER stamp on the simulated broker (UX-03).
    expect(form.querySelector('.ticket app-mode-stamp')!.textContent).toContain('PAPER');
    expect(form.textContent).toContain('no real money moves');
    expect(form.textContent).toContain('Go live with momentum-v3?');
    expect(form.textContent).toContain('Go-live check passed');
    expect(form.textContent).toContain('All 2 checks passed');
    // A passing promotion is confirmed by holding, not by typing the id.
    expect(isHoldDialog(el)).toBe(true);
    expect(form.querySelector('input')).toBeNull();
    expect(fillDialog(fixture, { reason: '' }).disabled).toBe(true);
    answerDialog(fixture, { reason: 'Paper period passed the gate' });

    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    expect(post.request.body).toEqual({ reason: 'Paper period passed the gate', override: false });
    post.flush({ ...DETAIL, status: 'active' });
    await reloaded('active');

    expect(success).toHaveBeenCalledWith(LIFECYCLE.live.done('momentum-v3'));
    expect(button(LIFECYCLE.live.label)).toBeUndefined();
    expect(button(LIFECYCLE.pause.label)).toBeDefined();
  });

  it('offers an override with a long reason when the gate refuses (409)', async () => {
    await load(DETAIL, HISTORY, { passed: true });
    const error = vi.spyOn(toasts, 'error');

    button(LIFECYCLE.live.label)!.click();
    await ticketReads(false);
    expect(dialogForm(el)!.textContent).toContain('3 days of paper trading, needs at least 20');
    answerDialog(fixture, { reason: 'Try it' });

    (await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST')).flush(
      { title: 'Conflict', status: 409, detail: 'go-live gate failed: min_days' },
      { status: 409, statusText: 'Conflict' },
    );
    await settle();

    const form = dialogForm(el)!;
    expect(form.textContent).toContain('The go-live gate refused momentum-v3');
    expect(form.textContent).toContain('Paper days');
    expect(error).not.toHaveBeenCalled();
    // A short reason or the wrong word keeps the override disabled.
    expect(fillDialog(fixture, { reason: 'too short', typed: 'override' }).disabled).toBe(true);
    expect(
      fillDialog(fixture, { reason: 'Owner accepts a short paper period', typed: 'nope' }).disabled,
    ).toBe(true);
    answerDialog(fixture, { reason: 'Owner accepts a short paper period', typed: 'override' });

    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    expect(post.request.body).toEqual({
      reason: 'Owner accepts a short paper period',
      override: true,
    });
    post.flush({ ...DETAIL, status: 'active' });
    await reloaded('active');
    expect(button(LIFECYCLE.pause.label)).toBeDefined();
  });

  it('stops only with a reason and does nothing when cancelled', async () => {
    await load();
    button(LIFECYCLE.stop.label)!.click();
    await settle();
    const form = dialogForm(el)!;
    expect(form.querySelector('button.btn-danger')).not.toBeNull();
    expect(fillDialog(fixture, { reason: '   ' }).disabled).toBe(true);

    Array.from(form.querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === 'Cancel')!
      .click();
    await settle();
    expect(dialogForm(el)).toBeNull();
    expect(controller.match(() => true)).toEqual([]);

    button(LIFECYCLE.stop.label)!.click();
    await settle();
    answerDialog(fixture, { reason: 'Edge is gone' });
    const post = await nextRequest(controller, '/api/strategies/momentum-v3/retire', 'POST');
    expect(post.request.body).toEqual({ reason: 'Edge is gone', override: false });
    post.flush({ ...DETAIL, status: 'retired' });
    await reloaded('retired');
  });

  it('shows the API message when the strategy is missing', async () => {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(
      { title: 'Not Found', status: 404, detail: 'no strategy with id momentum-v3' },
      { status: 404, statusText: 'Not Found' },
    );
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page([]));
    (await nextRequest(controller, '/api/orders')).flush(page([]));
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain(
      'no strategy with id momentum-v3',
    );
  });

  describe('permissions (UI-06)', () => {
    it('disables the status actions with a reason for non-admins', async () => {
      allowed.set(false);
      await load(DETAIL, HISTORY, { passed: true });
      for (const label of [LIFECYCLE.live.label, LIFECYCLE.stop.label]) {
        expect(button(label)!.disabled).toBe(true);
      }
      expect(el.querySelector('app-page-header app-permission-note')?.textContent).toContain(
        'Admins only.',
      );
      button(LIFECYCLE.live.label)!.click();
      await settle();
      expect(dialogForm(el)).toBeNull();
    });

    it('shows no permission note for admins', async () => {
      await load(DETAIL, HISTORY, { passed: true });
      expect(button(LIFECYCLE.live.label)!.disabled).toBe(false);
      expect(el.querySelector('app-permission-note')).toBeNull();
    });
  });

  describe('stage bar (UI-11)', () => {
    const current = () => el.querySelector('app-stage-bar [aria-current="step"]')?.textContent;

    it('shows Paper and links to the go-live check while the gate fails', async () => {
      await load();
      expect(current()).toContain('Paper');
      const link = el.querySelector<HTMLAnchorElement>('app-stage-bar a')!;
      expect(link.getAttribute('href')).toBe('/go-live?strategy=momentum-v3');
    });

    it('shows Ready once the gate passes, with Go live in the header only', async () => {
      await load(DETAIL, HISTORY, { passed: true });
      expect(current()).toContain('Ready');
      expect(el.querySelector('app-stage-bar button')).toBeNull();
      expect(button(LIFECYCLE.live.label)).toBeDefined();
    });

    it('folds the failing checks under the bar', async () => {
      await load();
      expect(el.querySelector('app-stage-bar details.checks')!.textContent).toContain(
        '3 days of paper trading, needs at least 20',
      );
    });

    it('shows Live for an active strategy and links to its orders', async () => {
      await load({ ...DETAIL, status: 'active' });
      expect(current()).toContain('Live');
      expect(el.querySelector('app-stage-bar a')?.getAttribute('href')).toBe('/orders');
      expect(button(LIFECYCLE.pause.label)).toBeDefined();
    });
  });

  describe('performance and orders (UI-16)', () => {
    it('shows the paper return, drawdown and days with a value chart', async () => {
      await load();
      const perf = el.querySelector('.perf')!.textContent!;
      expect(perf).toContain('+8.00%');
      expect(perf).toContain('-5.00%');
      expect(perf).toContain('21');
      const summary = el.querySelector('app-time-series-chart')!.textContent!;
      expect(summary).toContain('a return of +8.00%');
      await settle();
      expect(chart.last?.map((s) => s.id)).toEqual(['value', 'drawdown']);
      expect(chart.last?.[0].points).toHaveLength(3);
    });

    it('explains the missing chart for a live strategy', async () => {
      await load({ ...DETAIL, status: 'active' });
      expect(el.textContent).toContain('It trades live now.');
      expect(el.querySelector('app-time-series-chart')).toBeNull();
    });

    it('lists recent orders with the side mark', async () => {
      await load({ ...DETAIL, status: 'active' }, HISTORY, { orders: [ORDER] });
      const orders = el.querySelector('section[aria-labelledby="orders-title"]')!;
      expect(orders.textContent).toContain('AAPL.US');
      expect(orders.querySelector('app-side-tag')?.textContent).toContain('Buy');
    });

    it("asks for this strategy's orders only", async () => {
      (await nextRequest(controller, '/api/strategies/momentum-v3')).flush({
        ...DETAIL,
        status: 'retired',
      });
      (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page([]));
      const req = await nextRequest(controller, '/api/orders');
      expect(req.request.urlWithParams).toContain('strategy_id=momentum-v3');
      req.flush(page([]));
      await settle();
      expect(el.textContent).toContain('No orders yet');
    });

    it('computes performance from the P&L rows', () => {
      controller.match(() => true); // the page's own reads are not needed here
      expect(paperPerformance([])).toBeNull();
      expect(paperPerformance(PNL)).toMatchObject({
        totalReturn: 0.08,
        maxDrawdown: -0.05,
        days: 21,
      });
    });
  });
  it('opens the Model versions tab and keeps it in the address', async () => {
    await load();
    const tab = Array.from(el.querySelectorAll<HTMLButtonElement>('app-segmented button')).find(
      (b) => b.textContent?.trim() === 'Model versions',
    )!;
    tab.click();
    await settle();
    (await nextRequest(controller, '/api/strategies/momentum-v3/versions')).flush(page([]));
    (await nextRequest(controller, '/api/strategies/momentum-v3/versions/history')).flush(page([]));
    await settle();
    expect(el.querySelector('app-model-versions-panel')).toBeTruthy();
    expect(el.querySelector('#perf-title')).toBeNull();
    expect(el.textContent).toContain('No model versions');
  });

  it('opens on the versions tab from ?tab=versions', async () => {
    fixture.componentRef.setInput('tab', 'versions');
    fixture.detectChanges();
    await load();
    (await nextRequest(controller, '/api/strategies/momentum-v3/versions')).flush(page([]));
    (await nextRequest(controller, '/api/strategies/momentum-v3/versions/history')).flush(page([]));
    await settle();
    expect(el.querySelector('app-model-versions-panel')).toBeTruthy();
  });
});
