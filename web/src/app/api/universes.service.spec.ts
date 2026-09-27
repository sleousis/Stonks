import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest, page } from '../../testing/http';
import { provideApi } from './provide-api';
import { UniversesService } from './universes.service';

describe('UniversesService', () => {
  let controller: HttpTestingController;
  let universes: UniversesService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    universes = TestBed.inject(UniversesService);
  });

  afterEach(() => controller.verify());

  it('lists, creates and deletes', async () => {
    const list = universes.list();
    (await nextRequest(controller, '/api/universes')).flush(page([]));
    expect(await list).toEqual([]);

    const created = universes.create({ id: 'big', kind: 'list', spec: { tickers: ['A.US'] } });
    const post = await nextRequest(controller, '/api/universes', 'POST');
    expect(post.request.body).toEqual({ id: 'big', kind: 'list', spec: { tickers: ['A.US'] } });
    post.flush({ id: 'big', kind: 'list', spec: {} });
    await created;

    const deleted = universes.delete('big');
    (await nextRequest(controller, '/api/universes/big', 'DELETE')).flush({
      id: 'big',
      kind: 'list',
      spec: {},
    });
    await deleted;
  });

  it('edits a definition, reads membership history and the exchanges', async () => {
    const updated = universes.update('big', { kind: 'list', spec: { tickers: ['B.US'] } });
    const put = await nextRequest(controller, '/api/universes/big', 'PUT');
    expect(put.request.body).toEqual({ kind: 'list', spec: { tickers: ['B.US'] } });
    put.flush({ id: 'big', kind: 'list', spec: { tickers: ['B.US'] } });
    expect((await updated).spec).toEqual({ tickers: ['B.US'] });

    const history = universes.history('big', { ticker: 'aa', limit: 50, offset: 50 });
    const get = await nextRequest(controller, '/api/universes/big/history');
    expect(get.request.urlWithParams).toContain('ticker=aa');
    expect(get.request.urlWithParams).toContain('offset=50');
    get.flush({ items: [], total: 0, limit: 50, offset: 50 });
    expect((await history).total).toBe(0);

    const exchanges = universes.exchanges();
    (await nextRequest(controller, '/api/universes/exchanges')).flush([
      { exchange: 'US', instruments: 3, listed: 2 },
    ]);
    expect((await exchanges)[0].exchange).toBe('US');
  });

  it('reads members on a date', async () => {
    const members = universes.members('big', '2024-01-02');
    const req = await nextRequest(controller, '/api/universes/big/members');
    expect(req.request.urlWithParams).toContain('as_of=2024-01-02');
    req.flush({ universe_id: 'big', as_of: '2024-01-02', tickers: ['A.US'], count: 1 });
    expect((await members).count).toBe(1);
  });

  it('starts refresh and ensure jobs and reads their results', async () => {
    const refresh = universes.refresh('big');
    (await nextRequest(controller, '/api/universes/big/refresh', 'POST')).flush({ id: 'job_r' });
    expect((await refresh).id).toBe('job_r');
    const refreshed = universes.refreshResult('job_r');
    (await nextRequest(controller, '/api/universes/refresh/job_r/result')).flush({
      universe_id: 'big',
    });
    await refreshed;

    const ensure = universes.ensure('big', { start: '2025-01-01', end: '2025-12-31' });
    const post = await nextRequest(controller, '/api/universes/big/ensure', 'POST');
    expect(post.request.body).toEqual({ start: '2025-01-01', end: '2025-12-31' });
    post.flush({ id: 'job_e' });
    await ensure;
    const report = universes.ensureResult('job_e');
    (await nextRequest(controller, '/api/universes/ensure/job_e/result')).flush({
      source: 'eodhd',
    });
    await report;
  });

  it('imports an index history', async () => {
    const view = universes.importIndexHistory({ index_id: 'sp500', content: 'date,ticker,action' });
    const post = await nextRequest(controller, '/api/universes/index-history', 'POST');
    expect(post.request.body).toEqual({ index_id: 'sp500', content: 'date,ticker,action' });
    post.flush({ index_id: 'sp500', as_of: null, constituents: 0, changes: 0 });
    await view;
  });
});
