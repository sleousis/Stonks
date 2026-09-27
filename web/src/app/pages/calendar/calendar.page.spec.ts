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
import { CalendarPage } from './calendar.page';
import { addDays } from './calendar-view';

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
        actual: null,
        estimate: 2.9,
        previous: 3.0,
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
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
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
    expect(el.querySelector('h2#cal-events-title')?.textContent).toContain('Earnings reports');
    expect(el.textContent).toContain('After the close');
    expect(el.querySelector('a[href="/charts/AAPL.US"]')).not.toBeNull();
    // The tabs count each calendar.
    expect(radio(el, 'Earnings').textContent).toContain('(1)');
    expect(radio(el, 'Ex-dividend').textContent).toContain('(1)');
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
    expect(el.textContent).toContain('CPI');
    expect(el.textContent).toContain('Year on year');

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
