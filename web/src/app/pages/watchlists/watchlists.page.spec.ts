import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { WatchlistView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { WatchlistsPage, labLink, quoteFrom } from './watchlists.page';

const TECH: WatchlistView = {
  id: 'wl_1',
  name: 'Tech',
  tickers: ['AAPL.US', 'MSFT.US'],
  created_at: '2026-09-27T00:00:00Z',
  updated_at: '2026-09-27T00:00:00Z',
};

describe('WatchlistsPage', () => {
  let http: HttpTestingController;
  const confirm = vi.fn();

  beforeEach(async () => {
    confirm.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => {
    flushBars();
    http.verify();
  });

  /** Answer every pending bars read: closes 100 then 102 (+2%). */
  function flushBars(): void {
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/market/bars')) {
      const ticker = r.request.params.get('ticker') ?? new URL(r.request.urlWithParams, 'http://x').searchParams.get('ticker');
      r.flush({
        ticker,
        interval: '1d',
        truncated: false,
        bars: [
          { timestamp: '2026-10-08', open: 99, high: 101, low: 98, close: 100, adj_close: 100, volume: 1 },
          { timestamp: '2026-10-09', open: 100, high: 103, low: 99, close: 102, adj_close: 102, volume: 1 },
        ],
      });
    }
  }

  async function render(lists: WatchlistView[]) {
    const fixture = TestBed.createComponent(WatchlistsPage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/watchlists')).flush(page(lists));
    await tick();
    fixture.detectChanges();
    return fixture;
  }

  function button(el: HTMLElement, text: string): HTMLButtonElement {
    const b = [...el.querySelectorAll('button')].find((x) => x.textContent?.trim() === text);
    if (!b) throw new Error(`no "${text}" button`);
    return b;
  }

  it('links each ticker to its chart and the list to the lab', async () => {
    const fixture = await render([TECH]);
    const el: HTMLElement = fixture.nativeElement;
    const links = [...el.querySelectorAll('.ticker-link')].map((a) => a.getAttribute('href'));
    expect(links).toEqual(['/charts/AAPL.US', '/charts/MSFT.US']);
    const lab = [...el.querySelectorAll('a')].find((a) =>
      a.textContent?.includes('Open in the lab'),
    );
    expect(lab?.getAttribute('href')).toBe('/lab?tickers=AAPL.US,MSFT.US');
    expect(labLink(TECH)).toEqual({ tickers: 'AAPL.US,MSFT.US' });
  });

  it('shows each ticker with its last close, day change and a sparkline', async () => {
    const fixture = await render([TECH]);
    const el: HTMLElement = fixture.nativeElement;
    await tick(5);
    flushBars();
    await tick(5);
    fixture.detectChanges();
    const row = el.querySelector('tbody tr')!;
    expect(row.textContent).toContain('102');
    expect(row.textContent).toContain('+2.00%');
    expect(row.querySelector('app-sparkline svg')).not.toBeNull();
    expect(el.querySelector('[role="tab"]')?.textContent).toContain('Tech');
    expect(el.textContent).toContain('2 of 2 rose');
  });

  it('reads a quote from daily bars', () => {
    expect(quoteFrom('A', [{ close: 10 }, { close: 11, adj_close: 11 }])).toEqual({
      ticker: 'A',
      last: 11,
      change: expect.closeTo(0.1, 6),
      closes: [10, 11],
    });
    expect(quoteFrom('A', [])).toEqual({ ticker: 'A', last: null, change: null, closes: [] });
  });

  it('creates a watchlist from typed tickers', async () => {
    const fixture = await render([]);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('No watchlists yet');
    const name = el.querySelector<HTMLInputElement>('#wl-name')!;
    name.value = 'Tech';
    name.dispatchEvent(new Event('input'));
    const tickers = el.querySelector<HTMLTextAreaElement>('#wl-tickers')!;
    tickers.value = 'aapl.us, msft.us';
    tickers.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    button(el, 'Save watchlist').click();
    const req = await nextRequest(http, '/api/watchlists', 'POST');
    expect(req.request.body).toEqual({ name: 'Tech', tickers: ['AAPL.US', 'MSFT.US'] });
    req.flush(TECH);
    // The page and the shared filter both read the lists again.
    const reads = http.match((r) => r.url.split('?')[0] === '/api/watchlists');
    for (const r of reads) r.flush(page([TECH]));
    await tick(5);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/watchlists')) {
      r.flush(page([TECH]));
    }
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Tech');
  });

  it('asks before deleting, and drops the list as a filter', async () => {
    const ctx = TestBed.inject(WatchlistContextService);
    ctx.select('wl_1');
    confirm.mockResolvedValue(true);
    const fixture = await render([TECH]);
    const el: HTMLElement = fixture.nativeElement;
    button(el, 'Delete').click();
    await tick();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Delete Tech?', tone: 'danger' }),
    );
    (await nextRequest(http, '/api/watchlists/wl_1', 'DELETE')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    await tick(5);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/watchlists')) {
      r.flush(page([]));
    }
    await tick(5);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/watchlists')) {
      r.flush(page([]));
    }
    expect(ctx.selectedId()).toBeNull();
  });

  it('keeps the list when the delete is cancelled', async () => {
    confirm.mockResolvedValue(false);
    const fixture = await render([TECH]);
    button(fixture.nativeElement, 'Delete').click();
    await tick();
    http.expectNone((r) => r.method === 'DELETE');
  });
});
