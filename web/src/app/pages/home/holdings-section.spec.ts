import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PortfolioView, PositionView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { HoldingsSection } from './holdings-section';
import { HomeStore } from './home-store';

function position(overrides: Partial<PositionView>): PositionView {
  return {
    ticker: 'AAPL.US',
    quantity: 1,
    price: 100,
    price_date: '2026-09-25',
    market_value: 100,
    weight: 0.1,
    ...overrides,
  };
}

function portfolio(positions: PositionView[], extra: Partial<PortfolioView> = {}): PortfolioView {
  return {
    taken_at: '2026-09-25T21:00:00Z',
    tick_id: 't1',
    cash: 0,
    positions,
    positions_value: 0,
    total_value: 0,
    snapshot_total_value: 0,
    ...extra,
  };
}

describe('HoldingsSection', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting(), HomeStore],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(view: PortfolioView) {
    const fixture = TestBed.createComponent(HoldingsSection);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page([book({ id: 'pf_1', name: 'Main' })]));
    (await nextRequest(http, '/api/portfolio')).flush(view);
    (await nextRequest(http, '/api/pnl')).flush({ strategy_id: null, rows: [] });
    await tick(2);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('lists the biggest holding first, with a weight bar and the count', async () => {
    const el = await render(
      portfolio([
        position({ ticker: 'AAPL.US', market_value: 100, weight: 0.1 }),
        position({ ticker: 'MSFT.US', market_value: 300, weight: 0.3, unrealized_pnl_pct: 0.02 }),
      ]),
    );
    expect([...el.querySelectorAll('.ticker')].map((t) => t.textContent)).toEqual([
      'MSFT.US',
      'AAPL.US',
    ]);
    expect(el.querySelector('h2')?.textContent).toContain('Holdings');
    expect(el.querySelector('.count')?.textContent).toBe('2');
    expect(el.querySelector('.pnl')?.classList).toContain('gain');
    const bars = [...el.querySelectorAll<HTMLElement>('.bar')].map((b) =>
      parseFloat(b.style.width),
    );
    expect(bars[0]).toBeCloseTo(4.5);
    expect(bars[1]).toBeCloseTo(1.5);
  });

  it('rounds paper shares to two decimals (NB-14)', async () => {
    const el = await render(portfolio([position({ quantity: 139.9214, market_value: 139.92 })]));
    expect(el.querySelector('.qty')?.textContent).toBe('139.92 shares');
  });

  it('shows each holding in its own currency', async () => {
    const el = await render(
      portfolio([position({ ticker: 'SAP.XETRA', market_value: 300, currency: 'EUR' })], {
        currency: 'EUR',
      }),
    );
    expect(el.textContent).toContain('€300.00');
    expect(el.textContent).not.toContain('$300.00');
  });

  it('says how holdings arrive when there are none', async () => {
    const el = await render(portfolio([]));
    expect(el.textContent).toContain('No holdings yet');
  });
});
