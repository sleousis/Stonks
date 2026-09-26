import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { StrategyDetail } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { StrategyDetailPage } from './strategy-detail.page';

const DETAIL: StrategyDetail = {
  id: 'momentum-v3',
  status: 'shadow',
  class_path: 'stonks.strategies.momentum.MomentumStrategy',
  applicable_asset_classes: ['equity', 'crypto'],
  params: { lookback: 126, top_n: 5, universe: ['AAPL.US', 'MSFT.US'] },
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-20T10:00:00Z',
  survival_reports: [
    { test_id: 'oos', passed: true, notes: 'held up out of sample', metrics: { cagr_oos: 0.1234 } },
    { test_id: 'drift', passed: false, notes: '', metrics: { drift: 0.31, limit: null } },
  ],
};

describe('StrategyDetailPage', () => {
  let fixture: ComponentFixture<StrategyDetailPage>;
  let controller: HttpTestingController;
  let confirm: ConfirmService;
  let toasts: ToastService;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [StrategyDetailPage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    confirm = TestBed.inject(ConfirmService);
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

  async function load(detail = DETAIL): Promise<void> {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(detail);
    await settle();
  }

  function button(label: string): HTMLButtonElement | undefined {
    return Array.from(el.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
  }

  it('renders class, params, asset classes, registered date and survival reports', async () => {
    await load();
    const text = el.textContent ?? '';

    expect(el.querySelector('h1')?.textContent).toContain('momentum-v3');
    expect(text).toContain('MomentumStrategy');
    expect(text).toContain('stonks.strategies.momentum.MomentumStrategy');
    expect(text).toContain('equity');
    expect(text).toContain('crypto');
    expect(text).toContain('2026-09-01');
    expect(text).toContain('lookback');
    expect(text).toContain('126');
    expect(text).toContain('["AAPL.US","MSFT.US"]');

    const reports = el.querySelectorAll('.report');
    expect(reports.length).toBe(2);
    expect(reports[0].textContent).toContain('oos');
    expect(reports[0].textContent).toContain('pass');
    expect(reports[0].textContent).toContain('0.1234');
    expect(reports[1].textContent).toContain('fail');
    expect(reports[1].classList).toContain('failed');
    expect(text).toContain('1 of 2 passed');

    expect(el.querySelector('a[href="/lab?strategy=momentum-v3"]')).not.toBeNull();
    expect(el.querySelector('a[href="/go-live?strategy=momentum-v3"]')).not.toBeNull();
  });

  it('offers only the actions that change the status', async () => {
    await load();
    expect(button('Promote to active')).toBeDefined();
    expect(button('Retire')).toBeDefined();
    expect(button('Move to shadow')).toBeUndefined();
  });

  it('promotes after a typed confirmation, toasts and reloads', async () => {
    await load();
    const success = vi.spyOn(toasts, 'success');

    button('Promote to active')!.click();
    const request = confirm.request();
    expect(request?.typedConfirmation).toBe('momentum-v3');
    expect(request?.confirmLabel).toBe('Promote');
    request!.resolve(true);

    const post = await nextRequest(controller, '/api/strategies/momentum-v3/promote', 'POST');
    post.flush({ ...DETAIL, status: 'active' });
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush({
      ...DETAIL,
      status: 'active',
    });
    await settle();

    expect(success).toHaveBeenCalledWith('Promoted momentum-v3.');
    expect(button('Promote to active')).toBeUndefined();
    expect(button('Move to shadow')).toBeDefined();
  });

  it('retires behind a danger confirmation and does nothing when cancelled', async () => {
    await load();
    button('Retire')!.click();
    const request = confirm.request();
    expect(request?.tone).toBe('danger');
    expect(request?.typedConfirmation).toBeUndefined();
    request!.resolve(false);
    await settle();
    expect(controller.match(() => true)).toEqual([]);
  });

  it('shows the API message when the strategy is missing', async () => {
    (await nextRequest(controller, '/api/strategies/momentum-v3')).flush(
      { title: 'Not Found', status: 404, detail: 'no strategy with id momentum-v3' },
      { status: 404, statusText: 'Not Found' },
    );
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain(
      'no strategy with id momentum-v3',
    );
  });
});
