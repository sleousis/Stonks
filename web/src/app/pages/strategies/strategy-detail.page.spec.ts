import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { StatusChangeView, StrategyDetail } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { answerDialog, dialogForm, fillDialog, goLiveReport } from '../../../testing/status-dialog';
import { STRATEGY_METADATA } from '../../../testing/strategy-fixtures';
import { StrategyDetailPage, historyEntries } from './strategy-detail.page';

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

describe('StrategyDetailPage', () => {
  let fixture: ComponentFixture<StrategyDetailPage>;
  let controller: HttpTestingController;
  let toasts: ToastService;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [StrategyDetailPage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
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

  async function load(detail = DETAIL, history = HISTORY): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(detail);
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(history);
    await settle();
  }

  async function reloaded(status: StrategyDetail['status']): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush({ ...DETAIL, status });
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(HISTORY);
    await settle();
  }

  function button(label: string): HTMLButtonElement | undefined {
    return Array.from(el.querySelectorAll<HTMLButtonElement>('app-page-header button')).find(
      (b) => b.textContent?.trim() === label,
    );
  }

  it('renders class, params, research card and survival reports', async () => {
    await load();
    const text = el.textContent ?? '';

    expect(el.querySelector('h1')?.textContent).toContain('momentum-v3');
    expect(text).toContain('MomentumStrategy');
    expect(text).toContain('equity');
    expect(text).toContain('2026-09-01');
    expect(text).toContain('lookback');
    expect(text).toContain('["AAPL.US","MSFT.US"]');

    const research = el.querySelector('section[aria-labelledby="research-title"]')!;
    expect(research.querySelector('blockquote')?.textContent).toContain(
      'Recent winners keep winning',
    );
    expect(research.textContent).toContain('Trend');
    expect(research.textContent).toContain('21 bars');

    const reports = el.querySelectorAll('.report');
    expect(reports.length).toBe(2);
    expect(reports[0].textContent).toContain('CAGR (held out)');
    expect(reports[0].textContent).toContain('12.34%');
    expect(reports[1].textContent).toContain('n/a');
    expect(reports[1].classList).toContain('failed');
    expect(text).toContain('1 of 2 passed');

    expect(el.querySelector('a[href="/lab?strategy=momentum-v3"]')).not.toBeNull();
    expect(el.querySelector('a[href="/go-live?strategy=momentum-v3"]')).not.toBeNull();
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
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(DETAIL);
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush(
      { title: 'Server error', status: 500, detail: 'boom' },
      { status: 500, statusText: 'Server Error' },
    );
    await settle();
    expect(el.textContent).toContain('Could not load the status history');
    expect(el.querySelectorAll('.report').length).toBe(2);
  });

  it('offers only the actions that change the status', async () => {
    await load();
    expect(button('Promote to active')).toBeDefined();
    expect(button('Retire')).toBeDefined();
    expect(button('Move to shadow')).toBeUndefined();
  });

  it('promotes with a reason after showing the go-live result', async () => {
    await load();
    const success = vi.spyOn(toasts, 'success');

    button('Promote to active')!.click();
    (await nextRequest(controller, '/api/strategies/momentum-v3/golive')).flush(
      goLiveReport('momentum-v3', true),
    );
    await settle();

    const form = dialogForm(el)!;
    expect(form.textContent).toContain('Go-live check passed');
    expect(form.textContent).toContain('All 2 checks passed');
    // Needs both a reason and the typed id.
    expect(fillDialog(fixture, { reason: '', typed: 'momentum-v3' }).disabled).toBe(true);
    answerDialog(fixture, { reason: 'Paper period passed the gate', typed: 'momentum-v3' });

    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    expect(post.request.body).toEqual({ reason: 'Paper period passed the gate', override: false });
    post.flush({ ...DETAIL, status: 'active' });
    await reloaded('active');

    expect(success).toHaveBeenCalledWith('Promoted momentum-v3.');
    expect(button('Promote to active')).toBeUndefined();
    expect(button('Move to shadow')).toBeDefined();
  });

  it('offers an override with a long reason when the gate refuses (409)', async () => {
    await load();
    const error = vi.spyOn(toasts, 'error');

    button('Promote to active')!.click();
    (await nextRequest(controller, '/api/strategies/momentum-v3/golive')).flush(
      goLiveReport('momentum-v3', false),
    );
    await settle();
    expect(dialogForm(el)!.textContent).toContain('3 paper day(s), need >= 20');
    answerDialog(fixture, { reason: 'Try it', typed: 'momentum-v3' });

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
    expect(button('Move to shadow')).toBeDefined();
  });

  it('retires only with a reason and does nothing when cancelled', async () => {
    await load();
    button('Retire')!.click();
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

    button('Retire')!.click();
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
    (await nextRequest(controller, '/api/strategies/momentum-v3/history')).flush([]);
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain(
      'no strategy with id momentum-v3',
    );
  });
});
