import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { OrdersService } from '../../api/orders.service';
import { PortfolioService } from '../../api/portfolio.service';
import { provideApi } from '../../api/provide-api';
import { nextRequest, page } from '../../../testing/http';
import { TRADER } from '../../../testing/auth-fixtures';
import { SessionService } from '../auth/session.service';
import { PortfolioContextService } from './portfolio-context.service';

const LIST = [
  { id: 'pf_default', name: 'Main', mode: 'paper', is_default: true },
  { id: 'pf_b', name: 'Second', mode: 'live' },
];

describe('PortfolioContextService', () => {
  let ctx: PortfolioContextService;
  let controller: HttpTestingController;

  beforeEach(() => {
    localStorage.removeItem('stonks.portfolio');
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    ctx = TestBed.inject(PortfolioContextService);
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  async function load(body: object | null = LIST, status = 200) {
    const loading = ctx.load();
    const req = await nextRequest(controller, '/api/portfolios');
    if (status === 200) req.flush(Array.isArray(body) ? page(body) : body);
    else req.flush({ title: 'x', status }, { status, statusText: 'x' });
    await loading;
  }

  it('lists portfolios and offers a choice when there is more than one', async () => {
    await load();
    expect(ctx.state()).toBe('ready');
    expect(ctx.hasChoice()).toBe(true);
    expect(ctx.current()?.id).toBe('pf_default');
    expect(ctx.query()).toEqual({});
  });

  it('sends the picked portfolio and remembers it', async () => {
    await load();
    ctx.select('pf_b');
    expect(ctx.selectedId()).toBe('pf_b');
    expect(ctx.query()).toEqual({ portfolio_id: 'pf_b' });
    expect(localStorage.getItem('stonks.portfolio')).toBe('pf_b');
    ctx.select(null);
    expect(ctx.query()).toEqual({});
    expect(localStorage.getItem('stonks.portfolio')).toBeNull();
  });

  it('drops a remembered portfolio that is gone', async () => {
    localStorage.setItem('stonks.portfolio', 'pf_old');
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    ctx = TestBed.inject(PortfolioContextService);
    controller = TestBed.inject(HttpTestingController);
    expect(ctx.query()).toEqual({});
    await load();
    expect(ctx.selectedId()).toBeNull();
    expect(localStorage.getItem('stonks.portfolio')).toBeNull();
  });

  it('knows when the user has no portfolio (UX-13)', async () => {
    expect(ctx.noBook()).toBe(false);
    await load([]);
    expect(ctx.noBook()).toBe(true);
    expect(ctx.hasBook()).toBe(false);
  });

  it('counts a server without the list route as having a book', async () => {
    await load(null, 404);
    expect(ctx.hasBook()).toBe(true);
    expect(ctx.noBook()).toBe(false);
  });

  it('stays hidden on a server without the route', async () => {
    await load(null, 404);
    expect(ctx.state()).toBe('missing');
    expect(ctx.hasChoice()).toBe(false);
    expect(ctx.query()).toEqual({});
  });

  it('first list 500, second ok: live() becomes true (UX-15)', async () => {
    await load(null, 500);
    expect(ctx.state()).toBe('failed');
    expect(ctx.live()).toBe(false);
    await load([{ id: 'pf_live', name: 'Real', trading: 'live', is_default: true }]);
    expect(ctx.state()).toBe('ready');
    expect(ctx.live()).toBe(true);
  });

  it('retries a failed read on its own, after a wait (UX-15)', async () => {
    vi.useFakeTimers();
    try {
      void ctx.load();
      await vi.advanceTimersByTimeAsync(0);
      controller
        .expectOne((r) => r.url.startsWith('/api/portfolios'))
        .flush({ title: 'x', status: 500 }, { status: 500, statusText: 'x' });
      await vi.advanceTimersByTimeAsync(0);
      expect(ctx.state()).toBe('failed');
      controller.expectNone((r) => r.url.startsWith('/api/portfolios'));
      await vi.advanceTimersByTimeAsync(2000);
      controller
        .expectOne((r) => r.url.startsWith('/api/portfolios'))
        .flush(page([{ id: 'pf_live', name: 'Real', trading: 'live', is_default: true }]));
      await vi.advanceTimersByTimeAsync(0);
      expect(ctx.live()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('load(true) starts a new read while one is running (UX-15)', async () => {
    const first = ctx.load();
    const stale = await nextRequest(controller, '/api/portfolios');
    const second = ctx.load(true);
    const fresh = await nextRequest(controller, '/api/portfolios');
    fresh.flush(page([{ id: 'pf_new', name: 'New', is_default: true }]));
    await second;
    stale.flush(page(LIST));
    await first;
    expect(ctx.options().map((p) => p.id)).toEqual(['pf_new']);
  });

  it('the stored pick is per user (UX-07)', async () => {
    localStorage.setItem('stonks.portfolio.usr_1', 'pf_b');
    localStorage.setItem('stonks.portfolio.usr_admin', 'pf_default');
    const signIn = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await signIn;
    await load();
    expect(ctx.selectedId()).toBe('pf_b');
    ctx.select(null);
    expect(localStorage.getItem('stonks.portfolio.usr_1')).toBeNull();
    expect(localStorage.getItem('stonks.portfolio.usr_admin')).toBe('pf_default');
    localStorage.removeItem('stonks.portfolio.usr_admin');
  });

  it('adds portfolio_id to portfolio, P&L, order and fill reads', async () => {
    await load();
    ctx.select('pf_b');
    const sent = async (path: string, body: object) => {
      const req = await nextRequest(controller, path);
      expect(req.request.urlWithParams, path).toContain('portfolio_id=pf_b');
      req.flush(body);
    };
    const page = { items: [], total: 0, offset: 0, limit: 50 };
    void TestBed.inject(PortfolioService).get();
    await sent('/api/portfolio', {});
    void TestBed.inject(PortfolioService).pnl();
    await sent('/api/pnl', { rows: [] });
    void TestBed.inject(OrdersService).list({ limit: 5 });
    await sent('/api/orders', page);
    void TestBed.inject(OrdersService).fills();
    await sent('/api/orders/fills', page);
  });
});
