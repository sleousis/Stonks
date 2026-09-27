import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest, page } from '../../testing/http';
import { RESULT, SAVED } from '../../testing/screener-fixtures';
import { provideApi } from './provide-api';
import { ScreenerService } from './screener.service';

describe('ScreenerService', () => {
  let http: HttpTestingController;
  let api: ScreenerService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    api = TestBed.inject(ScreenerService);
  });

  afterEach(() => http.verify());

  it('runs a spec on a date', async () => {
    const run = api.run({ spec: { limit: 5 }, as_of: '2026-09-25' });
    const req = await nextRequest(http, '/api/screener/run', 'POST');
    expect(req.request.body).toEqual({ spec: { limit: 5 }, as_of: '2026-09-25' });
    req.flush(RESULT);
    expect((await run).matched).toBe(2);
  });

  it('lists, reads, saves, changes and deletes screens', async () => {
    const list = api.list();
    (await nextRequest(http, '/api/screener/screens')).flush(page([SAVED]));
    expect(await list).toEqual([SAVED]);

    const get = api.get('scr_1');
    (await nextRequest(http, '/api/screener/screens/scr_1')).flush(SAVED);
    expect((await get).name).toBe('Cheap payers');

    const create = api.create({ name: 'A', spec: {} });
    (await nextRequest(http, '/api/screener/screens', 'POST')).flush(SAVED);
    await create;

    const update = api.update('scr_1', { name: 'B' });
    const patch = await nextRequest(http, '/api/screener/screens/scr_1', 'PATCH');
    expect(patch.request.body).toEqual({ name: 'B' });
    patch.flush(SAVED);
    await update;

    const del = api.delete('scr_1');
    (await nextRequest(http, '/api/screener/screens/scr_1', 'DELETE')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    await del;
  });

  it('saves a screen as a universe silently', async () => {
    const save = api.saveAsUniverse({ universe_id: 'u1', screen_id: 'scr_1', mode: 'rule' });
    const req = await nextRequest(http, '/api/screener/universes', 'POST');
    req.flush({ universe: { id: 'u1', kind: 'rule', spec: {} }, warnings: [] });
    expect((await save).universe.id).toBe('u1');
  });

  it('lists the metrics', async () => {
    const metrics = api.metrics();
    (await nextRequest(http, '/api/screener/metrics')).flush([]);
    expect(await metrics).toEqual([]);
  });
});
