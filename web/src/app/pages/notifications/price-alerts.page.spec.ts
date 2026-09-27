import type { Mock } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PriceAlertEventView, PriceAlertView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { PriceAlertsPage } from './price-alerts.page';

function alert(over: Partial<PriceAlertView> = {}): PriceAlertView {
  return {
    id: 'pa_1',
    name: null,
    target_kind: 'ticker',
    ticker: 'AAA.US',
    watchlist_id: null,
    condition: 'crosses_above',
    level: 250,
    pct: null,
    window_days: null,
    enabled: true,
    created_at: '2026-09-20T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
    ...over,
  };
}

function firing(over: Partial<PriceAlertEventView> = {}): PriceAlertEventView {
  return {
    id: 1,
    rule_id: 'pa_2',
    ticker: 'BBB.US',
    price: 51.5,
    detail: 'BBB.US moved 9% in 5 days',
    observed_at: '2026-09-25',
    created_at: '2026-09-25T22:00:00Z',
    ...over,
  };
}

const RULES = [
  alert(),
  alert({
    id: 'pa_2',
    name: 'Tech swings',
    target_kind: 'watchlist',
    ticker: null,
    watchlist_id: 'wl_1',
    condition: 'moves_pct',
    level: null,
    pct: 8,
    window_days: 5,
    enabled: false,
  }),
];

describe('PriceAlertsPage', () => {
  let fixture: ComponentFixture<PriceAlertsPage>;
  let http: HttpTestingController;
  let confirm: Mock<(o: ConfirmOptions) => Promise<boolean>>;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
    confirm = vi.fn<(o: ConfirmOptions) => Promise<boolean>>(async () => true);
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockImplementation(confirm);
  });

  afterEach(() => http.verify());

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  async function render(rules = RULES, events = [firing()]) {
    fixture = TestBed.createComponent(PriceAlertsPage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/price-alerts')).flush(page(rules));
    (await nextRequest(http, '/api/watchlists')).flush(
      page([
        {
          id: 'wl_1',
          name: 'Tech',
          tickers: ['BBB.US'],
          created_at: '2026-09-01T00:00:00Z',
          updated_at: '2026-09-01T00:00:00Z',
        },
      ]),
    );
    (await nextRequest(http, '/api/price-alerts/events')).flush({
      items: events,
      total: events.length,
      limit: 25,
      offset: 0,
    });
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  const rows = (el: HTMLElement) => [...el.querySelectorAll<HTMLElement>('.alerts li')];
  const button = (row: HTMLElement, text: string) =>
    [...row.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent?.trim().startsWith(text),
    )!;

  it('lists each alert in plain words with the firings below', async () => {
    const el = await render();
    expect(el.querySelector('app-notifications-tabs')).not.toBeNull();
    const [first, second] = rows(el);
    expect(first.textContent).toContain('AAA.US');
    expect(first.textContent).toContain('Rises above 250');
    expect(second.textContent).toContain('Tech swings');
    expect(second.textContent).toContain('Every ticker in Tech');
    expect(second.textContent).toContain('Moves 8% either way in 5 days');
    expect(second.classList).toContain('off');
    const events = el.querySelector('app-price-alert-events')!;
    expect(events.textContent).toContain('BBB.US moved 9% in 5 days');
    expect(events.textContent).toContain('Tech swings');
  });

  it('says how alerts start when there are none', async () => {
    const el = await render([], []);
    expect(el.textContent).toContain('No price alerts yet');
    expect(el.textContent).toContain('No firings yet');
  });

  it('switches an alert on', async () => {
    const el = await render();
    rows(el)[1].querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    const req = await nextRequest(http, '/api/price-alerts/pa_2', 'PATCH');
    expect(req.request.body).toEqual({ enabled: true });
    req.flush({ ...RULES[1], enabled: true });
    (await nextRequest(http, '/api/price-alerts')).flush(page(RULES));
    await settle();
  });

  it('asks first, then deletes an alert', async () => {
    const el = await render();
    button(rows(el)[0], 'Delete').click();
    const req = await nextRequest(http, '/api/price-alerts/pa_1', 'DELETE');
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Delete the alert AAA.US?', tone: 'danger' }),
    );
    req.flush(null, { status: 204, statusText: 'No Content' });
    (await nextRequest(http, '/api/price-alerts')).flush(page([RULES[1]]));
    (await nextRequest(http, '/api/price-alerts/events')).flush({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
    });
    await settle();
    expect(rows(el)).toHaveLength(1);
  });

  it('opens an alert in the editor and saves the change', async () => {
    const el = await render();
    button(rows(el)[0], 'Change').click();
    fixture.detectChanges();
    expect(el.querySelector('#alert-editor-title')?.textContent).toContain('Change the alert');
    const level = el.querySelector<HTMLInputElement>('#pa-level')!;
    level.value = '260';
    level.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    el.querySelector<HTMLButtonElement>('app-price-alert-editor button[type="submit"]')!.click();
    (await nextRequest(http, '/api/price-alerts/pa_1', 'PATCH')).flush(alert({ level: 260 }));
    (await nextRequest(http, '/api/price-alerts')).flush(page([alert({ level: 260 })]));
    await settle();
    expect(el.querySelector('#alert-editor-title')?.textContent).toContain('New price alert');
  });

  it('filters the firings to one alert', async () => {
    const el = await render();
    const select = el.querySelector<HTMLSelectElement>('#pa-events-rule')!;
    select.value = 'pa_2';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/price-alerts/events');
    expect(req.request.urlWithParams).toContain('rule_id=pa_2');
    req.flush({ items: [firing()], total: 1, limit: 25, offset: 0 });
    await settle();
  });
});
