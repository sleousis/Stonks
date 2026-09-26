import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';
import { nextRequest } from '../../testing/http';
import { provideApi } from './provide-api';
import { TcaService } from './tca.service';

describe('TcaService', () => {
  let controller: HttpTestingController;
  let tca: TcaService;
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
    tca = TestBed.inject(TcaService);
  });

  afterEach(() => controller.verify());

  it('reads the summary with the grouping and the picked portfolio', async () => {
    portfolioId = 'pf_2';
    const summary = tca.summary({ by: 'ticker' });
    const req = await nextRequest(controller, '/api/tca/summary');
    expect(req.request.urlWithParams).toContain('by=ticker');
    expect(req.request.urlWithParams).toContain('portfolio_id=pf_2');
    req.flush({ by: 'ticker', groups: [], portfolio_id: 'pf_2', since: null, until: null });
    expect((await summary).by).toBe('ticker');
  });

  it('sends no portfolio for the default one', async () => {
    const summary = tca.summary();
    const req = await nextRequest(controller, '/api/tca/summary');
    expect(req.request.urlWithParams).not.toContain('portfolio_id');
    req.flush({ by: 'all', groups: [], portfolio_id: 'pf_default', since: null, until: null });
    await summary;
  });

  it('pages the journal for the picked portfolio', async () => {
    portfolioId = 'pf_2';
    const journal = tca.journal({ limit: 25, offset: 50 });
    const req = await nextRequest(controller, '/api/tca/journal');
    expect(req.request.urlWithParams).toContain('limit=25');
    expect(req.request.urlWithParams).toContain('offset=50');
    expect(req.request.urlWithParams).toContain('portfolio_id=pf_2');
    req.flush({ items: [], total: 0, limit: 25, offset: 50 });
    expect((await journal).total).toBe(0);
  });

  it('reads one order, adds a note and edits a note', async () => {
    const order = tca.order('2026-09-25:m:AAPL.US:buy');
    const o = await nextRequest(controller, '/api/tca/orders/2026-09-25%3Am%3AAAPL.US%3Abuy');
    o.flush({ client_id: 'x' });
    await order;

    const add = tca.addNote('abc', 'Filled late.');
    const a = await nextRequest(controller, '/api/tca/orders/abc/notes', 'POST');
    expect(a.request.body).toEqual({ note: 'Filled late.' });
    a.flush({ id: 7 }, { status: 201, statusText: 'Created' });
    expect((await add).id).toBe(7);

    const edit = tca.editNote(7, 'Filled late, wide spread.');
    const e = await nextRequest(controller, '/api/tca/notes/7', 'PUT');
    expect(e.request.body).toEqual({ note: 'Filled late, wide spread.' });
    e.flush({ id: 7 });
    await edit;
  });
});
