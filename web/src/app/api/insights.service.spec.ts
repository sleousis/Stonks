import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';
import { nextRequest } from '../../testing/http';
import { InsightsService } from './insights.service';
import { provideApi } from './provide-api';
import { RiskService } from './risk.service';

describe('InsightsService and RiskService', () => {
  let controller: HttpTestingController;
  let insights: InsightsService;
  let risk: RiskService;
  let portfolioId: string | null;

  beforeEach(() => {
    portfolioId = null;
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        {
          provide: PortfolioContextService,
          useValue: { query: () => (portfolioId ? { portfolio_id: portfolioId } : {}) },
        },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    insights = TestBed.inject(InsightsService);
    risk = TestBed.inject(RiskService);
  });

  afterEach(() => controller.verify());

  it('reads insights and agreement for the picked portfolio', async () => {
    portfolioId = 'pf_2';
    const got = insights.get();
    const req = await nextRequest(controller, '/api/insights');
    expect(req.request.urlWithParams).toContain('portfolio_id=pf_2');
    req.flush({ portfolio_id: 'pf_2' });
    expect((await got).portfolio_id).toBe('pf_2');

    const agreement = insights.agreement();
    const a = await nextRequest(controller, '/api/insights/agreement');
    expect(a.request.urlWithParams).toContain('portfolio_id=pf_2');
    a.flush({ portfolio_id: 'pf_2', holdings: [], strategies: [], skipped: [], as_of: null });
    await agreement;
  });

  it('reads admin totals without a portfolio', async () => {
    portfolioId = 'pf_2';
    const totals = insights.totals();
    const req = await nextRequest(controller, '/api/insights/totals');
    expect(req.request.urlWithParams).not.toContain('portfolio_id');
    req.flush({ portfolios: 2 });
    expect((await totals).portfolios).toBe(2);
  });

  it('reads live risk and pages its history for the picked portfolio', async () => {
    portfolioId = 'pf_2';
    const live = risk.live();
    const l = await nextRequest(controller, '/api/risk/live');
    expect(l.request.urlWithParams).toContain('portfolio_id=pf_2');
    l.flush({ portfolio_id: 'pf_2', as_of: null, portfolio: null, strategies: [] });
    await live;

    const snaps = risk.snapshots({ limit: 25, offset: 25 });
    const s = await nextRequest(controller, '/api/risk/snapshots');
    expect(s.request.urlWithParams).toContain('offset=25');
    expect(s.request.urlWithParams).toContain('portfolio_id=pf_2');
    s.flush({ items: [], total: 0, limit: 25, offset: 25 });
    expect((await snaps).total).toBe(0);
  });

  it('sends no portfolio for the default one', async () => {
    const got = insights.get();
    const req = await nextRequest(controller, '/api/insights');
    expect(req.request.urlWithParams).not.toContain('portfolio_id');
    req.flush({ portfolio_id: 'pf_default' });
    await got;
  });
});
