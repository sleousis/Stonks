import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import type { Type } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DashboardPage } from '../../pages/dashboard/dashboard.page';
import { InsightsPage } from '../../pages/insights/insights.page';
import { RiskPage } from '../../pages/insights/risk.page';
import { OrdersPage } from '../../pages/orders/orders.page';
import { TradesPage } from '../../pages/trades/trades.page';
import { provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, page, tick } from '../../../testing/http';
import { bookState } from './no-book';

describe('money pages with no portfolio (UX-13)', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeChart(),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const ctx = TestBed.inject(PortfolioContextService);
    const loading = ctx.load();
    (await nextRequest(http, '/api/portfolios')).flush(page([]));
    await loading;
  });

  /** Reads of one portfolio: its value, P&L, insights, risk, orders and costs. */
  const bookRead = (url: string) =>
    /^\/api\/(portfolio(\/|$)|pnl|insights(\/|$)|risk\/(live|snapshots)|tca|orders)/.test(url);

  async function render(page: Type<unknown>) {
    const fixture = TestBed.createComponent(page);
    fixture.detectChanges();
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
    const asked = http.match(() => true);
    const urls = asked.map((r) => r.request.url.split('?')[0]);
    // Anything else the page asks (runs, health, limits) is not about a book.
    for (const r of asked) r.flush({}, { status: 404, statusText: 'Not found' });
    await tick(5);
    return { el: fixture.nativeElement as HTMLElement, urls };
  }

  for (const [name, component] of [
    ['Dashboard', DashboardPage],
    ['Insights', InsightsPage],
    ['Risk', RiskPage],
    ['Trade costs', TradesPage],
    ['Orders', OrdersPage],
  ] as const) {
    it(`${name} shows one action to open a paper portfolio and asks for no portfolio`, async () => {
      const { el, urls } = await render(component);
      expect(urls.filter(bookRead)).toEqual([]);
      const empty = el.querySelector('app-no-book');
      expect(empty?.textContent).toContain('No portfolio yet');
      const links = [...empty!.querySelectorAll('a')];
      expect(links.map((a) => a.textContent?.trim())).toEqual(['Open a paper portfolio']);
      expect(links[0].getAttribute('href')).toBe('/welcome?step=portfolio');
      expect(el.textContent).not.toMatch(/ask your admin/i);
    });
  }

  it('reads as pending while the list loads and ready when it has a book', () => {
    const ctx = {
      state: () => 'loading',
      noBook: () => false,
    } as unknown as PortfolioContextService;
    expect(bookState(ctx)).toBe('pending');
    expect(bookState({ state: () => 'ready', noBook: () => false } as never)).toBe('ready');
    expect(bookState({ state: () => 'idle', noBook: () => false } as never)).toBe('ready');
    expect(bookState({ state: () => 'ready', noBook: () => true } as never)).toBe('none');
  });
});
