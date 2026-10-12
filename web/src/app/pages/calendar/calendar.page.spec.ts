import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { CalendarView, WatchlistView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { isoDay } from '../../core/format/format';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { CALENDAR_VIEW_KEY, CalendarPage } from './calendar.page';
import { addDays } from './calendar-view';
import { provideFakeDataCoverage } from '../../../testing/fake-data-coverage';

const TECH: WatchlistView = {
  id: 'wl_1',
  name: 'Tech',
  tickers: ['AAPL.US'],
  created_at: '2026-09-27T00:00:00Z',
  updated_at: '2026-09-27T00:00:00Z',
};

/** The query string the SDK wrote into the URL. */
function qs(req: TestRequest): URLSearchParams {
  return new URL(req.request.urlWithParams, 'http://x').searchParams;
}

function view(over: Partial<CalendarView> = {}): CalendarView {
  return {
    scope: 'holdings',
    start: '2026-09-27',
    end: '2026-10-27',
    tickers: ['AAPL.US'],
    earnings: [
      {
        ticker: 'AAPL.US',
        name: 'Apple',
        period_end: '2026-09-30',
        report_date: '2026-10-29',
        before_after_market: 'after',
        eps_estimate: 1.6,
        currency: 'USD',
        eps_actual: null,
        surprise_percent: null,
      },
    ],
    dividends: [
      { ticker: 'KO.US', ex_date: '2026-10-01', amount: 0.51, currency: 'USD', pay_date: null },
    ],
    economic: [
      {
        country: 'US',
        event_time: '2026-10-10T12:30:00Z',
        event_type: 'CPI',
        comparison: 'yoy',
        importance: 'high',
        actual: null,
        estimate: 2.9,
        previous: 3.0,
      },
      {
        country: 'US',
        event_time: '2026-10-02T12:30:00Z',
        event_type: 'Non Farm Payrolls',
        comparison: 'none',
        importance: 'high',
        actual: 210,
        estimate: 180,
        previous: 150,
      },
      {
        country: 'US',
        event_time: '2026-10-02T12:30:00Z',
        event_type: 'Unemployment Rate',
        comparison: 'none',
        importance: 'medium',
        actual: 4.1,
        estimate: 4.2,
        previous: 4.2,
      },
    ],
    truncated: false,
    ...over,
  };
}

describe('CalendarPage', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideFakeDataCoverage(),
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  async function render(inputs: Record<string, string> = {}) {
    const fixture = TestBed.createComponent(CalendarPage);
    for (const [k, v] of Object.entries(inputs)) fixture.componentRef.setInput(k, v);
    fixture.detectChanges();
    (await nextRequest(http, '/api/watchlists')).flush(page([TECH]));
    await tick();
    fixture.detectChanges();
    return fixture;
  }

  async function settle(fixture: { detectChanges(): void }) {
    await tick();
    fixture.detectChanges();
  }

  function radio(el: HTMLElement, text: string): HTMLButtonElement {
    const b = [...el.querySelectorAll<HTMLButtonElement>('[role=radio]')].find((x) =>
      x.textContent?.trim().startsWith(text),
    );
    if (!b) throw new Error(`no "${text}" option`);
    return b;
  }

  it('shows the earnings of your holdings for the next 30 days', async () => {
    const fixture = await render();
    const req = await nextRequest(http, '/api/calendars');
    const params = qs(req);
    expect(params.get('scope')).toBe('holdings');
    expect(params.get('start')).toBe(isoDay());
    expect(params.get('end')).toBe(addDays(isoDay(), 30));
    expect(params.has('countries')).toBe(false);
    req.flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    // Everything coming up, grouped by day, each event with its type.
    expect(el.querySelector('h2#cal-events-title')?.textContent).toContain('Coming up');
    expect(el.querySelectorAll('.day-row').length).toBe(4);
    const kinds = [...el.querySelectorAll('.event .kind')].map((k) => k.textContent?.trim());
    expect(kinds).toEqual(['Ex-dividend', 'Economic', 'Economic', 'Economic', 'Earnings']);
    expect(el.textContent).toContain('After the close');
    expect(radio(el, 'All').textContent).toContain('(5)');
    radio(el, 'Earnings').click();
    fixture.detectChanges();
    expect(el.querySelector('h2#cal-events-title')?.textContent).toContain('Earnings reports');
    expect(el.querySelector('a[href="/charts/AAPL.US"]')).not.toBeNull();
    // The tabs count each calendar.
    expect(radio(el, 'Earnings').textContent).toContain('(1)');
    expect(radio(el, 'Ex-dividend').textContent).toContain('(1)');
  });

  it('shows one kind as the sortable table it was, 50 a page (old calendar.page.ts:248)', async () => {
    localStorage.removeItem(CALENDAR_VIEW_KEY);
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    radio(el, 'Economic').click();
    fixture.detectChanges();
    expect(el.querySelector('table')).toBeNull();
    radio(el, 'Table').click();
    fixture.detectChanges();
    const heads = [...el.querySelectorAll('thead th')];
    expect(heads.map((th) => th.textContent?.trim())).toEqual(
      expect.arrayContaining(['Time', 'Release', 'Country', 'Importance']),
    );
    // Sortable column headers, first sorted by time.
    expect(heads[0].querySelector('button.sort')).not.toBeNull();
    expect(heads[0].getAttribute('aria-sort')).toBe('ascending');
    expect(el.querySelectorAll('tbody tr').length).toBe(3);
    expect(localStorage.getItem(CALENDAR_VIEW_KEY)).toBe('table');
    // All stays the agenda.
    radio(el, 'All').click();
    fixture.detectChanges();
    expect(el.querySelector('table')).toBeNull();
    expect(el.querySelectorAll('.day-row').length).toBe(4);
    localStorage.removeItem(CALENDAR_VIEW_KEY);
  });

  it('shows SEC filings of the last 30 days with a link to each document', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    radio(el, 'Filings').click();
    fixture.detectChanges();
    // Filings are past events: the tab reads the last 30 days, not the next 30.
    const req = await nextRequest(http, '/api/calendars');
    expect(qs(req).get('start')).toBe(addDays(isoDay(), -30));
    expect(qs(req).get('end')).toBe(isoDay());
    expect(el.querySelector<HTMLInputElement>('#cal-start')!.value).toBe(addDays(isoDay(), -30));
    req.flush(
      view({
        filings: [
          {
            ticker: 'AAPL.US',
            name: 'Apple',
            form: '8-K',
            accepted_at: '2026-09-20T20:05:00Z',
            items: ['2.02', '9.01'],
            item_names: ['Results of operations', 'Financial statements'],
            url: 'https://www.sec.gov/Archives/edgar/data/320193/x.htm',
          },
          {
            ticker: 'MSFT.US',
            form: '8-K',
            accepted_at: '2026-09-21T12:00:00Z',
            items: ['5.02'],
            item_names: [],
            url: 'javascript:alert(1)',
          },
        ],
      }),
    );
    await settle(fixture);
    expect(radio(el, 'Filings').textContent).toContain('(2)');
    expect(el.querySelector('h2#cal-events-title')?.textContent).toContain('SEC filings');
    expect(el.textContent).toContain('Results of operations');
    expect(el.textContent).toContain('Item 5.02');
    const links = [...el.querySelectorAll<HTMLAnchorElement>('a.filing-link')];
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      'https://www.sec.gov/Archives/edgar/data/320193/x.htm',
    ]);
    expect(links[0].getAttribute('rel')).toContain('noopener');
    expect(links[0].getAttribute('target')).toBe('_blank');

    // Back on Earnings, the next 30 days show again.
    radio(el, 'Earnings').click();
    fixture.detectChanges();
    expect(el.querySelector<HTMLInputElement>('#cal-start')!.value).toBe(isoDay());
  });

  it('shows EPS as money in the report currency', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('$1.60');
  });

  it('says why filings are empty: they are past events', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    radio(el, 'Filings').click();
    fixture.detectChanges();
    (await nextRequest(http, '/api/calendars')).flush(view({ filings: [] }));
    await settle(fixture);
    expect(el.textContent).toContain('No filings in these days');
    expect(el.textContent).toContain('earlier From date');
  });

  it('switches to ex-dividend dates and economic releases, and filters countries', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;

    radio(el, 'Ex-dividend').click();
    fixture.detectChanges();
    expect(el.textContent).toContain('KO.US');

    radio(el, 'Economic').click();
    fixture.detectChanges();
    expect(el.textContent).toContain('CPI (year on year)');
    // Values carry their unit, and no separate Compared column.
    expect(el.textContent).toContain('2.9%');
    expect(el.textContent).toContain('210K jobs');
    expect(el.textContent).toContain('4.1%');
    expect(el.textContent).not.toContain('Compared');

    const countries = el.querySelector<HTMLInputElement>('#cal-countries')!;
    countries.value = 'us, de';
    countries.dispatchEvent(new Event('input'));
    countries.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/calendars');
    expect(qs(req).get('countries')).toBe('US,DE');
    req.flush(view());
    await settle(fixture);
  });

  it('asks for nothing until tickers are named, then reads their events', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    radio(el, 'Tickers').click();
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Name some tickers');
    http.expectNone((r) => r.url === '/api/calendars');

    const input = el.querySelector<HTMLInputElement>('#cal-tickers')!;
    input.value = 'msft.us nvda.us';
    input.dispatchEvent(new Event('input'));
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/calendars');
    expect(qs(req).get('scope')).toBe('tickers');
    expect(qs(req).get('tickers')).toBe('MSFT.US,NVDA.US');
    req.flush(view({ scope: 'tickers', earnings: [] }));
    await settle(fixture);
    radio(el, 'Earnings').click();
    fixture.detectChanges();
    expect(el.textContent).toContain('No earnings in these days');
  });

  it('reads one watchlist when picked', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    radio(el, 'Watchlists').click();
    fixture.detectChanges();
    (await nextRequest(http, '/api/calendars')).flush(view({ scope: 'watchlists' }));
    await settle(fixture);
    const select = el.querySelector<HTMLSelectElement>('#cal-watchlist')!;
    expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
      'All your watchlists',
      'Tech',
    ]);
    select.value = 'wl_1';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/calendars');
    expect(qs(req).get('watchlist_id')).toBe('wl_1');
    req.flush(view({ scope: 'watchlists' }));
    await settle(fixture);
  });

  it('refuses a window over 120 days without asking the server', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    const end = el.querySelector<HTMLInputElement>('#cal-end')!;
    end.value = addDays(isoDay(), 200);
    end.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    await tick();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Pick at most 120 days');
    http.expectNone((r) => r.url === '/api/calendars');
  });

  it('opens on one ticker from a date (the event alert link)', async () => {
    const fixture = await render({ ticker: 'aapl.us', date: '2026-10-29' });
    const req = await nextRequest(http, '/api/calendars');
    expect(qs(req).get('scope')).toBe('tickers');
    expect(qs(req).get('tickers')).toBe('AAPL.US');
    expect(qs(req).get('start')).toBe('2026-10-29');
    req.flush(view({ scope: 'tickers' }));
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector<HTMLInputElement>('#cal-tickers')!.value).toBe('AAPL.US');
  });

  it('opens on the releases of one country (the economic alert link)', async () => {
    const fixture = await render({ country: 'us', date: '2026-10-10' });
    const req = await nextRequest(http, '/api/calendars');
    expect(qs(req).get('countries')).toBe('US');
    expect(qs(req).get('start')).toBe('2026-10-10');
    req.flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('CPI');
    expect(el.textContent).toContain('High');
    expect(el.querySelector<HTMLInputElement>('#cal-countries')!.value).toBe('US');
  });

  it('shows the news of the scope, and none for everything', async () => {
    const fixture = await render();
    (await nextRequest(http, '/api/calendars')).flush(view());
    await settle(fixture);
    const el: HTMLElement = fixture.nativeElement;
    radio(el, 'News').click();
    fixture.detectChanges();
    const news = await nextRequest(http, '/api/calendars/news');
    expect(qs(news).get('scope')).toBe('holdings');
    news.flush({ tickers: ['AAPL.US'], items: [], sentiment: [] });
    await settle(fixture);
    expect(el.textContent).toContain('No news yet');

    radio(el, 'Everything').click();
    fixture.detectChanges();
    (await nextRequest(http, '/api/calendars')).flush(view({ scope: 'all' }));
    await settle(fixture);
    expect(el.textContent).toContain('Pick whose news to show');
    http.expectNone((r) => r.url === '/api/calendars/news');
  });
});
