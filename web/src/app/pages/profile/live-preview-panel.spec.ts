import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { LivePreviewView, PreviewOrderView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { nextRequest, tick } from '../../../testing/http';
import { LivePreviewPanel, commissionText, previewOutcome } from './live-preview-panel';

const ORDER: PreviewOrderView = {
  client_id: '2026-09-28:pf_live:s1:AAPL.US:buy',
  ticker: 'AAPL.US',
  side: 'buy',
  quantity: 5,
  order_type: 'limit',
  limit_price: 201.5,
  time_in_force: 'opg',
  position_effect: 'open',
  strategy_id: 's1',
  notional: 1007.5,
  what_if: {
    initial_margin_change: 0,
    maintenance_margin_change: 0,
    equity_with_loan_after: 9000,
    commission: 1,
    commission_currency: 'USD',
    warning: null,
  },
  what_if_error: null,
  adjustments: [
    {
      ticker: 'AAPL.US',
      side: 'buy',
      rule: 'price_band',
      original_quantity: 5,
      adjusted_quantity: 5,
      reason: 'limit set within the band',
    },
  ],
};

const PREVIEW: LivePreviewView = {
  portfolio_id: 'pf_live',
  as_of: '2026-09-28',
  stage: 'broker_paper',
  status: 'ok',
  reason: null,
  orders: [ORDER],
  adjustments: [
    ...ORDER.adjustments,
    {
      ticker: 'TSLA.US',
      side: 'buy',
      rule: 'live_notional_caps',
      original_quantity: 3,
      adjusted_quantity: 0,
      reason: 'over the daily cap',
    },
  ],
  account: {
    equity: 10000,
    cash: 10000,
    settled_cash: 9000,
    available_funds: 9000,
    buying_power: 9000,
    currency: 'USD',
    account_type: 'cash',
  },
  allocation: 5000,
  what_if_available: true,
  transmitted: false,
  notes: [],
};

describe('live preview words', () => {
  it('says what the dry run made of the book', () => {
    expect(previewOutcome(PREVIEW)).toBe('1 order would go out.');
    expect(previewOutcome({ status: 'noop', orders: [], reason: 'no_signal' })).toBe(
      'Nothing to send: no signal.',
    );
    expect(previewOutcome({ status: 'error', orders: [], reason: 'broker down' })).toContain(
      'failed',
    );
  });

  it('shows the broker fee or why it is unknown', () => {
    expect(commissionText(ORDER)).toContain('1.00');
    expect(commissionText({ ...ORDER, what_if: null })).toBe('Not given');
    expect(commissionText({ ...ORDER, what_if: null, what_if_error: 'timed out' })).toBe(
      'Check failed: timed out',
    );
  });
});

describe('LivePreviewPanel', () => {
  let fixture: ComponentFixture<LivePreviewPanel>;
  let http: HttpTestingController;
  let allowed: boolean;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() => (allowed ? null : 'Traders only.'));
  });

  afterEach(() => http.verify());

  function render(): HTMLElement {
    fixture = TestBed.createComponent(LivePreviewPanel);
    fixture.componentRef.setInput('portfolioId', 'pf_live');
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function button(el: HTMLElement): HTMLButtonElement {
    return el.querySelector<HTMLButtonElement>('.actions button')!;
  }

  it('asks for nothing until you press preview, then shows what would go out', async () => {
    const el = render();
    http.expectNone('/api/portfolios/pf_live/live/preview');
    button(el).click();
    const post = await nextRequest(http, '/api/portfolios/pf_live/live/preview', 'POST');
    post.flush(PREVIEW);
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="preview-outcome"]')?.textContent).toContain(
      '1 order would go out.',
    );
    expect(el.textContent).toContain('AAPL.US');
    expect(el.textContent).toContain('price_band');
    expect(el.querySelector('.dropped')?.textContent).toContain('over the daily cap');
    expect(el.querySelector('[data-testid="preview-not-sent"]')).not.toBeNull();
    expect(button(el).textContent?.trim()).toBe('Preview again');
  });

  it('shows the error and lets you try again', async () => {
    const el = render();
    button(el).click();
    const post = await nextRequest(http, '/api/portfolios/pf_live/live/preview', 'POST');
    post.flush(
      { title: 'Unprocessable', status: 422, detail: 'no live book' },
      { status: 422, statusText: 'Unprocessable Entity' },
    );
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Could not preview the orders');
  });

  it('is off for someone without trade rights', () => {
    allowed = false;
    const el = render();
    expect(button(el).disabled).toBe(true);
  });
});
