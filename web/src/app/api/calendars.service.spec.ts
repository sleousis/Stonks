import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { CalendarsService } from './calendars.service';
import { provideApi } from './provide-api';

describe('CalendarsService', () => {
  let http: HttpTestingController;
  let api: CalendarsService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    api = TestBed.inject(CalendarsService);
  });

  afterEach(() => http.verify());

  it('reads the calendar with the scope, window and countries', async () => {
    const read = api.calendar({
      scope: 'tickers',
      tickers: 'AAPL.US',
      start: '2026-09-27',
      end: '2026-10-27',
      countries: 'US',
    });
    const req = await nextRequest(http, '/api/calendars');
    const url = req.request.urlWithParams;
    for (const part of ['scope=tickers', 'tickers=AAPL.US', 'start=2026-09-27', 'countries=US']) {
      expect(url).toContain(part);
    }
    req.flush({
      scope: 'tickers',
      start: '2026-09-27',
      end: '2026-10-27',
      tickers: ['AAPL.US'],
      earnings: [],
      dividends: [],
      economic: [],
    });
    expect((await read).scope).toBe('tickers');
  });

  it('reads news for a watchlist', async () => {
    const read = api.news({ scope: 'watchlists', watchlist_id: 'wl_1', limit: 20 });
    const req = await nextRequest(http, '/api/calendars/news');
    expect(req.request.urlWithParams).toContain('limit=20');
    req.flush({ tickers: [], items: [], sentiment: [] });
    expect((await read).items).toEqual([]);
  });

  it('checks earnings for the ticket and lists alert kinds, both silently', async () => {
    const check = api.earningsWarnings(['AAPL.US', 'MSFT.US']);
    const req = await nextRequest(http, '/api/calendars/earnings-warnings');
    expect(req.request.urlWithParams).toContain('tickers=AAPL.US%2CMSFT.US');
    req.flush({ checked: ['AAPL.US', 'MSFT.US'], warnings: [] });
    expect((await check).checked).toHaveLength(2);

    const kinds = api.alertKinds();
    const k = await nextRequest(http, '/api/calendars/alert-kinds');
    k.flush([]);
    expect(await kinds).toEqual([]);
  });
});
