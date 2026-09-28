import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, input, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type {
  BrokerInfo,
  GoLiveReport,
  OrderView,
  PnlRowView,
  StatusChangeView,
  StrategyDetail,
  TearSheetView,
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
import { STRATEGY_METADATA, paper } from '../../../testing/strategy-fixtures';
import { LIFECYCLE } from '../../shared/governance-labels';
import { WhyNotPanel } from '../../shared/ui/why-not-panel';
import { FollowPanel } from './follow-panel';
import {
  StrategyDetailPage,
  actorName,
  asTab,
  detailActions,
  historyEntries,
  paperPerformance,
  statusActions,
} from './strategy-detail.page';

/** The follow panel has its own spec; here only where it shows matters. */
@Component({ selector: 'app-follow-panel', template: 'Follow panel' })
class FollowPanelStub {
  readonly strategyId = input.required<string>();
  readonly strategyName = input<string | null>(null);
  readonly status = input<string | null>(null);
}

/** The why-not panel has its own spec. */
@Component({ selector: 'app-why-not-panel', template: 'Why not' })
class WhyNotPanelStub {
  readonly strategyId = input<string | null>(null);
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
    actor: 'service:system',
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

function sheetFor(
  detail: StrategyDetail,
  opts: { golive?: GoLiveReport | null; curve?: PnlRowView[] } = {},
): TearSheetView {
  const curve = opts.curve ?? PNL;
  return {
    strategy: detail,
    paper: paper({
      days: curve.length ? 21 : 0,
      total_return: curve.length ? 0.08 : null,
      max_drawdown: curve.length ? -0.05 : null,
      trades: 2,
    }),
    curve,
    monthly_returns: [],
    recent_trades: [],
    book_trades: 0,
    live_since: null,
    golive:
      opts.golive !== undefined
        ? opts.golive
        : detail.status === 'retired'
          ? null
          : goLiveReport(detail.id, false),
    status_history: [],
  };
}

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
      remove: { imports: [FollowPanel, WhyNotPanel] },
      add: { imports: [FollowPanelStub, WhyNotPanelStub] },
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

  /** Flush the page's reads: the strategy, its history, its trial record and orders. */
  async function load(
    detail = DETAIL,
    history = HISTORY,
    opts: {
      passed?: boolean;
      golive?: GoLiveReport | null;
      curve?: PnlRowView[];
      orders?: OrderView[];
      broker?: BrokerInfo;
    } = {},
  ): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(detail);
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page(history));
    (await nextRequest(controller, '/api/strategies/momentum-v3/tearsheet')).flush(
      sheetFor(detail, {
        golive:
          opts.golive !== undefined
            ? opts.golive
            : opts.passed !== undefined
              ? goLiveReport(detail.id, opts.passed)
              : undefined,
        curve: opts.curve,
      }),
    );
    (await nextRequest(controller, '/api/orders')).flush(page(opts.orders ?? []));
    await settle();
    if (detail.status === 'active') {
      (await nextRequest(controller, '/api/brokers')).flush(opts.broker ?? SIMULATED);
      await settle();
    }
  }

  async function reloaded(status: StrategyDetail['status']): Promise<void> {
    const detail = { ...DETAIL, status };
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(detail);
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page(HISTORY));
    (await nextRequest(controller, '/api/strategies/momentum-v3/tearsheet')).flush(
      sheetFor(detail, { golive: status === 'retired' ? null : goLiveReport(detail.id, true) }),
    );
    await settle();
    if (status === 'active') {
      (await nextRequest(controller, '/api/brokers')).flush(SIMULATED);
      await settle();
    }
  }

  /** The reads behind the approval ticket: check, broker and followers. */
  async function ticketReads(passed: boolean, broker: BrokerInfo = SIMULATED): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3/golive')).flush(
      goLiveReport('momentum-v3', passed),
    );
    (await nextRequest(controller, '/api/brokers')).flush(broker);
    (await nextRequest(controller, '/api/subscriptions')).flush(page([]));
    await settle();
  }

  function button(label: string, scope = 'app-page-header'): HTMLButtonElement | undefined {
    return Array.from(el.querySelectorAll<HTMLButtonElement>(`${scope} button`)).find(
      (b) => b.textContent?.trim() === label,
    );
  }

  async function openTab(label: string): Promise<void> {
    Array.from(el.querySelectorAll<HTMLButtonElement>('app-page-tabs button'))
      .find((b) => b.textContent?.trim() === label)!
      .click();
    await settle();
  }

  it('opens on a plain verdict with the reasons and the figures under Details (F33)', async () => {
    await load(DETAIL, HISTORY, { passed: false });
    const verdict = el.querySelector('app-strategy-verdict')!;
    expect(verdict.textContent).toContain('Promising, needs more data');
    expect(verdict.textContent).toContain('It has 3 of the 20 trial days it needs.');
    const details = verdict.querySelector('details')!;
    expect(details.querySelector('summary')!.textContent).toContain('Details');
    expect(details.textContent).toContain('3 against ≥ 20');
    expect(details.textContent).toContain('Robustness tests: 1 of 2 passed.');
  });

  it('shows the trial result from its test book with a value chart', async () => {
    await load();
    const perf = el.querySelector('.perf')!.textContent!;
    expect(perf).toContain('+8.00%');
    expect(perf).toContain('-5.00%');
    expect(perf).toContain('21');
    expect(el.querySelector('#perf-title')!.textContent).toBe('Trial results');
    const summary = el.querySelector('app-time-series-chart')!.textContent!;
    expect(summary).toContain('a return of +8.00%');
    await settle();
    expect(chart.last?.map((s) => s.id)).toEqual(['value', 'drawdown']);
    expect(chart.last?.[0].points).toHaveLength(3);
  });

  it('shows an approved strategy its trial record, never an empty page (F28)', async () => {
    await load({ ...DETAIL, status: 'active' }, HISTORY, { passed: true });
    expect(el.querySelector('.perf')!.textContent).toContain('+8.00%');
    expect(el.querySelector('app-strategy-verdict')!.textContent).toContain('Worth following');
    expect(el.textContent).not.toMatch(/trades live now/i);
  });

  it('explains an approved strategy with no trial record', async () => {
    await load({ ...DETAIL, status: 'active' }, HISTORY, { passed: true, curve: [] });
    expect(el.textContent).toContain('No trial record');
    expect(el.textContent).toContain('Follow it on Paper');
    expect(el.querySelector('#perf-title + * app-time-series-chart')).toBeNull();
  });

  it('never calls the strategy live and uses the ladder words (B1)', async () => {
    await load({ ...DETAIL, status: 'active' });
    const subtitle = el.querySelector('app-page-header p')?.textContent ?? '';
    expect(subtitle).toContain('Momentum strategy. Approved.');
    const copy = el.cloneNode(true) as HTMLElement;
    copy.querySelectorAll('.timeline .reason').forEach((r) => r.remove());
    expect(copy.textContent).not.toMatch(/(?<!go-)\blive\b|shadow|promot|regist/i);
    expect(el.querySelector('app-stage-bar [aria-current="step"]')?.textContent).toContain(
      'Approved',
    );
  });

  it('shows names, not raw ids, and keeps the id under Technical details (UX-27)', async () => {
    await load({ ...DETAIL, id: 'momentum_3fa9c21b', status: 'retired' });
    expect(el.querySelector('h1')!.textContent).toBe('Momentum 3fa9');
    await openTab('Details');
    expect(el.querySelector('details.tech')!.textContent).toContain('momentum_3fa9c21b');
  });

  it('keeps research, parameters and robustness tests on the Details tab (F29)', async () => {
    await load();
    expect(el.querySelector('section[aria-labelledby="research-title"]')).toBeNull();
    await openTab('Details');
    const text = el.textContent ?? '';
    expect(text).toContain('Lookback');
    expect(text).toContain('Top n');
    expect(text).toContain('["AAPL.US","MSFT.US"]');
    const research = el.querySelector('section[aria-labelledby="research-title"]')!;
    expect(research.querySelector('blockquote')?.textContent).toContain(
      'Recent winners keep winning',
    );
    expect(research.textContent).toContain('21 periods');
    expect(research.textContent).not.toMatch(/\bbars?\b/);
    const reports = el.querySelectorAll('.report');
    expect(reports.length).toBe(2);
    const headings = [...el.querySelectorAll('.report h3')].map((h) => h.textContent?.trim());
    expect(headings[0]).toContain('Out of sample');
    expect(reports[0].textContent).toContain('12.34%');
    expect(reports[1].classList).toContain('failed');
    expect(el.querySelector('details.tech')?.textContent).toContain(
      'stonks.strategies.momentum.MomentumStrategy',
    );
  });

  it('puts the results of its test book on the Results tab, with the printable tear sheet', async () => {
    await load();
    await openTab('Results');
    const tiles = [...el.querySelectorAll('app-stat-tile')].map((t) => t.textContent ?? '');
    expect(tiles.join(' ')).toContain('Days on trial');
    expect(el.querySelector('a[href="/strategies/momentum-v3/tearsheet"]')?.textContent).toContain(
      'Printable tear sheet',
    );
    expect(el.textContent).toContain('No real money');
  });

  it('shows the go-live check and the status history newest first on Review', async () => {
    await load();
    await openTab('Review');
    const checks = el.querySelector('app-golive-check-list')!;
    expect(checks.textContent).toContain('Trial days');
    expect(checks.textContent).toContain('Before you approve');
    const items = el.querySelectorAll('.timeline li');
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain('Check overridden');
    expect(items[0].textContent).toContain('Go-live check failed');
    expect(items[0].classList).toContain('override');
    // The system is "Stonks", not a service id (F31).
    expect(items[1].textContent).toContain('By Stonks');
    expect(historyEntries(HISTORY).map((h) => h.id)).toEqual([2, 1]);
  });

  it('keeps the page when only the history fails', async () => {
    const retired = { ...DETAIL, status: 'retired' as const };
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(retired);
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(
      { title: 'Server error', status: 500, detail: 'boom' },
      { status: 500, statusText: 'Server Error' },
    );
    (await nextRequest(controller, '/api/strategies/momentum-v3/tearsheet')).flush(
      sheetFor(retired),
    );
    (await nextRequest(controller, '/api/orders')).flush(page([]));
    await settle();
    expect(el.querySelector('app-follow-panel')).toBeNull();
    await openTab('Review');
    expect(el.textContent).toContain('Could not load the status history');
  });

  it('offers to follow a strategy on trial or approved, not a retired one', async () => {
    await load();
    expect(el.querySelector('app-follow-panel')).not.toBeNull();
  });

  it('offers Approve in the header only once the check passed (UX-23)', async () => {
    await load(DETAIL, HISTORY, { passed: true });
    expect(button(LIFECYCLE.live.label)!.classList).toContain('btn-primary');
    expect(button('Override…')).toBeUndefined();
    // Retire is never in the header next to the kill switch (M9).
    expect(button(LIFECYCLE.stop.label)).toBeUndefined();
  });

  it('keeps Retire quiet on the Review tab, never red (M9)', async () => {
    await load();
    await openTab('Review');
    const retire = button(LIFECYCLE.stop.label, 'section[aria-labelledby="status-title"]')!;
    expect(retire.classList).not.toContain('btn-danger');
    expect(retire.classList).not.toContain('btn-primary');
    retire.click();
    await settle();
    const form = dialogForm(el)!;
    expect(form.textContent).toContain('Retire momentum-v3?');
    expect(form.querySelector('button.btn-danger')).toBeNull();
    expect(fillDialog(fixture, { reason: '   ' }).disabled).toBe(true);
    Array.from(form.querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === 'Cancel')!
      .click();
    await settle();
    expect(dialogForm(el)).toBeNull();
    expect(controller.match(() => true)).toEqual([]);

    button(LIFECYCLE.stop.label, 'section[aria-labelledby="status-title"]')!.click();
    await settle();
    answerDialog(fixture, { reason: 'Edge is gone' });
    const post = await nextRequest(controller, '/api/strategies/momentum-v3/retire', 'POST');
    expect(post.request.body).toEqual({ reason: 'Edge is gone', override: false });
    post.flush({ ...DETAIL, status: 'retired' });
    await reloaded('retired');
  });

  it('a strategy on trial that fails shows no Approve, admins get Override (UX-23)', async () => {
    await load();
    expect(button(LIFECYCLE.live.label)).toBeUndefined();
    const override = button('Override…')!;
    expect(override.classList).not.toContain('btn-primary');
    override.click();
    await ticketReads(false);
    const form = dialogForm(el)!;
    expect(form.textContent).toContain('without passing the check');
    expect(form.querySelector('.ticket')).not.toBeNull();
    // A paper broker: the words match the PAPER stamp, and no red button (B2).
    expect(form.textContent).toContain('No real money moves');
    expect(form.textContent).not.toMatch(/real orders/i);
    expect(form.querySelector('button.btn-danger')).toBeNull();
    answerDialog(fixture, { reason: 'Owner accepts a short paper period', typed: 'override' });
    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    expect(post.request.body).toEqual({
      reason: 'Owner accepts a short paper period',
      override: true,
    });
    post.flush({ ...DETAIL, status: 'active' });
    await reloaded('active');
  });

  it('a retired strategy offers only Put on trial (UX-23)', async () => {
    await load({ ...DETAIL, status: 'retired' });
    const labels = [...el.querySelectorAll('app-page-header button')].map((b) =>
      b.textContent?.trim(),
    );
    expect(labels).toEqual([LIFECYCLE.paper.label]);
  });

  it('shows the LIVE stamp by the title of an approved strategy on a real broker', async () => {
    await load({ ...DETAIL, status: 'active' }, HISTORY, { broker: REAL });
    expect(el.querySelector('app-page-header app-mode-stamp')!.textContent).toContain('LIVE');
  });

  it('shows no stamp for an approved strategy on a paper broker', async () => {
    await load({ ...DETAIL, status: 'active' });
    expect(el.querySelector('app-page-header app-mode-stamp')).toBeNull();
  });

  it('approves with a reason and a hold after showing the go-live check', async () => {
    await load(DETAIL, HISTORY, { passed: true });
    const success = vi.spyOn(toasts, 'success');

    button(LIFECYCLE.live.label)!.click();
    await ticketReads(true);

    const form = dialogForm(el)!;
    expect(form.querySelector('.ticket app-mode-stamp')!.textContent).toContain('PAPER');
    expect(form.textContent).toContain('Approval ticket');
    expect(form.textContent).toContain('No real money moves');
    expect(form.textContent).toContain('Approve momentum-v3?');
    expect(form.textContent).toContain('Go-live check passed');
    expect(isHoldDialog(el)).toBe(true);
    expect(form.querySelector('input')).toBeNull();
    expect(fillDialog(fixture, { reason: '' }).disabled).toBe(true);
    answerDialog(fixture, { reason: 'Trial passed the check' });

    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    expect(post.request.body).toEqual({ reason: 'Trial passed the check', override: false });
    post.flush({ ...DETAIL, status: 'active' });
    await reloaded('active');

    expect(success).toHaveBeenCalledWith(LIFECYCLE.live.done('momentum-v3'));
    expect(button(LIFECYCLE.live.label)).toBeUndefined();
  });

  it('offers an override with a long reason when the check refuses (409)', async () => {
    await load(DETAIL, HISTORY, { passed: true });
    const error = vi.spyOn(toasts, 'error');

    button(LIFECYCLE.live.label)!.click();
    await ticketReads(false);
    answerDialog(fixture, { reason: 'Try it' });

    (await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST')).flush(
      { title: 'Conflict', status: 409, detail: 'go-live gate failed: min_days' },
      { status: 409, statusText: 'Conflict' },
    );
    await settle();

    const form = dialogForm(el)!;
    expect(form.textContent).toContain('The go-live check refused momentum-v3');
    expect(form.textContent).toContain('Trial days');
    expect(error).not.toHaveBeenCalled();
    expect(fillDialog(fixture, { reason: 'too short', typed: 'override' }).disabled).toBe(true);
    answerDialog(fixture, { reason: 'Owner accepts a short paper period', typed: 'override' });

    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    expect(post.request.body).toEqual({
      reason: 'Owner accepts a short paper period',
      override: true,
    });
    post.flush({ ...DETAIL, status: 'active' });
    await reloaded('active');
  });

  it('puts an approved strategy back on trial from the Review tab', async () => {
    await load({ ...DETAIL, status: 'active' }, HISTORY, { passed: true });
    await openTab('Review');
    const scope = 'section[aria-labelledby="status-title"]';
    expect(button(LIFECYCLE.pause.label, scope)).toBeDefined();
    expect(button(LIFECYCLE.stop.label, scope)).toBeDefined();
  });

  it('shows the API message when the strategy is missing', async () => {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(
      { title: 'Not Found', status: 404, detail: 'no strategy with id momentum-v3' },
      { status: 404, statusText: 'Not Found' },
    );
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page([]));
    (await nextRequest(controller, '/api/strategies/momentum-v3/tearsheet')).flush(
      { title: 'Not Found', status: 404, detail: 'no strategy with id momentum-v3' },
      { status: 404, statusText: 'Not Found' },
    );
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
      expect(button(LIFECYCLE.live.label)!.disabled).toBe(true);
      expect(el.querySelector('app-page-header app-permission-note')?.textContent).toContain(
        'Admins only.',
      );
      button(LIFECYCLE.live.label)!.click();
      await settle();
      expect(dialogForm(el)).toBeNull();
    });

    it('hides the Model versions tab from those who cannot run the Lab (F29)', async () => {
      allowed.set(false);
      await load();
      const tabs = [...el.querySelectorAll('app-page-tabs button')].map((b) =>
        b.textContent?.trim(),
      );
      expect(tabs).toEqual(['Overview', 'Results', 'Review', 'Details']);
    });
  });

  describe('stage bar (UI-11)', () => {
    const current = () => el.querySelector('app-stage-bar [aria-current="step"]')?.textContent;

    it('shows On trial and links to the Review tab while the check fails', async () => {
      await load();
      expect(current()).toContain('On trial');
      const link = el.querySelector<HTMLAnchorElement>('app-stage-bar a')!;
      expect(link.getAttribute('href')).toBe('/strategies/momentum-v3?tab=review');
    });

    it('says ready to approve once the check passes, with Approve in the header only', async () => {
      await load(DETAIL, HISTORY, { passed: true });
      expect(current()).toContain('ready to approve');
      expect(el.querySelector('app-stage-bar button')).toBeNull();
      expect(button(LIFECYCLE.live.label)).toBeDefined();
    });
  });

  describe('orders', () => {
    it('lists recent orders with the side mark', async () => {
      await load({ ...DETAIL, status: 'active' }, HISTORY, { orders: [ORDER] });
      const orders = el.querySelector('section[aria-labelledby="orders-title"]')!;
      expect(orders.textContent).toContain('AAPL.US');
      expect(orders.querySelector('app-side-tag')?.textContent).toContain('Buy');
    });

    it("asks for this strategy's orders only", async () => {
      const retired = { ...DETAIL, status: 'retired' as const };
      (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(retired);
      (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(page([]));
      (await nextRequest(controller, '/api/strategies/momentum-v3/tearsheet')).flush(
        sheetFor(retired),
      );
      const req = await nextRequest(controller, '/api/orders');
      expect(req.request.urlWithParams).toContain('strategy_id=momentum-v3');
      req.flush(page([]));
      await settle();
      expect(el.textContent).toContain('No orders yet');
    });
  });

  it('opens the Model versions tab and keeps it in the address', async () => {
    await load();
    await openTab('Model versions');
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

describe('strategy page helpers', () => {
  it('offers one forward step in the header and quiet steps back elsewhere', () => {
    expect(detailActions('shadow', true, false).map((a) => a.label)).toEqual(['Approve']);
    expect(detailActions('shadow', false, true).map((a) => a.label)).toEqual(['Override…']);
    expect(detailActions('shadow', false, false)).toEqual([]);
    expect(detailActions('active', true, true)).toEqual([]);
    expect(detailActions('retired', null, true).map((a) => a.label)).toEqual(['Put on trial']);
    expect(statusActions('active').map((a) => a.label)).toEqual(['Back on trial', 'Retire']);
    expect(statusActions('shadow').map((a) => a.label)).toEqual(['Retire']);
    expect(statusActions('retired')).toEqual([]);
  });

  it('reads tabs from the address and names the system Stonks', () => {
    expect(asTab('review')).toBe('review');
    expect(asTab('nope')).toBe('overview');
    expect(asTab(undefined)).toBe('overview');
    expect(actorName('service:system')).toBe('Stonks');
    expect(actorName('user:ann')).toBe('ann');
  });

  it('computes performance from the P&L rows', () => {
    expect(paperPerformance([])).toBeNull();
    expect(paperPerformance(PNL)).toMatchObject({
      totalReturn: 0.08,
      maxDrawdown: -0.05,
      days: 21,
    });
  });
});
