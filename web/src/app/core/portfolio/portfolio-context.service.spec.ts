import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { OrdersService } from '../../api/orders.service';
import { PortfolioService } from '../../api/portfolio.service';
import { provideApi } from '../../api/provide-api';
import { nextRequest, page } from '../../../testing/http';
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

  it('stays hidden on a server without the route', async () => {
    await load(null, 404);
    expect(ctx.state()).toBe('missing');
    expect(ctx.hasChoice()).toBe(false);
    expect(ctx.query()).toEqual({});
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
