import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest, page } from '../../../testing/http';
import { WatchlistContextService, signalTicker } from './watchlist-context.service';

const TECH = {
  id: 'wl_1',
  name: 'Tech',
  tickers: ['AAPL.US'],
  created_at: '2026-09-27T00:00:00Z',
  updated_at: '2026-09-27T00:00:00Z',
};

describe('WatchlistContextService', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    localStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    localStorage.clear();
  });

  it('filters by the picked list and remembers the pick', async () => {
    const ctx = TestBed.inject(WatchlistContextService);
    const loading = ctx.load();
    (await nextRequest(http, '/api/watchlists')).flush(page([TECH]));
    await loading;
    expect(ctx.keeps('MSFT.US')).toBe(true);
    ctx.select('wl_1');
    expect(ctx.selected()?.name).toBe('Tech');
    expect(ctx.keeps('aapl.us')).toBe(true);
    expect(ctx.keeps('MSFT.US')).toBe(false);
    expect(localStorage.getItem('stonks.watchlist')).toBe('wl_1');
    ctx.select(null);
    expect(ctx.tickers()).toBeNull();
    expect(localStorage.getItem('stonks.watchlist')).toBeNull();
  });

  it('ignores a remembered list that is gone, and a failed read', async () => {
    localStorage.setItem('stonks.watchlist', 'wl_gone');
    const ctx = TestBed.inject(WatchlistContextService);
    const loading = ctx.load();
    (await nextRequest(http, '/api/watchlists')).flush(null, {
      status: 500,
      statusText: 'Server Error',
    });
    await loading;
    expect(ctx.state()).toBe('failed');
    expect(ctx.selected()).toBeNull();
    expect(ctx.keeps('ANY.US')).toBe(true);
  });

  it('reads the ticker from a signal title', () => {
    expect(signalTicker('aapl.us: entry signal')).toBe('AAPL.US');
    expect(signalTicker('Trading run finished')).toBeNull();
  });
});
