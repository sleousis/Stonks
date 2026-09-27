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
import { WatchlistsPage, labLink } from './watchlists.page';

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

  afterEach(() => http.verify());

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
    const chips = [...el.querySelectorAll('.chip')].map((a) => a.getAttribute('href'));
    expect(chips).toEqual(['/charts/AAPL.US', '/charts/MSFT.US']);
    const lab = [...el.querySelectorAll('a')].find((a) =>
      a.textContent?.includes('Open in the lab'),
    );
    expect(lab?.getAttribute('href')).toBe('/lab?tickers=AAPL.US,MSFT.US');
    expect(labLink(TECH)).toEqual({ tickers: 'AAPL.US,MSFT.US' });
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
