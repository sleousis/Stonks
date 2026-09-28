import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PriceAlertView, WatchlistView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { nextRequest, tick } from '../../../testing/http';
import { PriceAlertEditor } from './price-alert-editor';
import { conditionText, targetText } from './price-alert-text';

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

const TECH: WatchlistView = {
  id: 'wl_1',
  name: 'Tech',
  tickers: ['AAA.US', 'BBB.US'],
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
};

describe('PriceAlertEditor', () => {
  let fixture: ComponentFixture<PriceAlertEditor>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
    fixture = TestBed.createComponent(PriceAlertEditor);
    fixture.componentRef.setInput('watchlists', [TECH]);
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  const el = () => fixture.nativeElement as HTMLElement;
  function type(selector: string, value: string) {
    const input = el().querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }
  function pick(group: string, label: string) {
    [...el().querySelectorAll<HTMLButtonElement>(`[aria-label="${group}"] [role="radio"]`)]
      .find((b) => b.textContent?.trim() === label)!
      .click();
    fixture.detectChanges();
  }
  const submit = () => el().querySelector<HTMLButtonElement>('button[type="submit"]')!.click();

  it('says what is missing', async () => {
    submit();
    fixture.detectChanges();
    expect(el().querySelector('#pa-ticker-error')?.textContent).toContain('Enter a ticker');
    expect(el().querySelector('#pa-level-error')?.textContent).toContain('above zero');
    await tick(5);
    expect(http.match(() => true)).toHaveLength(0);
  });

  it('makes a crossing alert on one ticker', async () => {
    const saved = vi.fn();
    fixture.componentInstance.saved.subscribe(saved);
    type('#pa-ticker', 'aaa.us');
    pick('Fires when the price', 'Falls below');
    type('#pa-level', '180');
    submit();
    const req = await nextRequest(http, '/api/price-alerts', 'POST');
    expect(req.request.body).toEqual({
      condition: 'crosses_below',
      ticker: 'AAA.US',
      watchlist_id: null,
      level: 180,
      pct: null,
      window_days: null,
      name: null,
      enabled: true,
    });
    req.flush(alert({ condition: 'crosses_below', level: 180 }));
    await tick();
    expect(saved).toHaveBeenCalled();
    fixture.detectChanges();
    expect(el().querySelector<HTMLInputElement>('#pa-level')!.value).toBe('');
  });

  it('makes a move alert on a watchlist', async () => {
    pick('Watch', 'A watchlist');
    const select = el().querySelector<HTMLSelectElement>('#pa-watchlist')!;
    select.value = 'wl_1';
    select.dispatchEvent(new Event('change'));
    pick('Fires when the price', 'Moves by');
    type('#pa-pct', '8');
    type('#pa-days', '5');
    type('#pa-name', 'Tech swings');
    submit();
    const req = await nextRequest(http, '/api/price-alerts', 'POST');
    expect(req.request.body).toMatchObject({
      condition: 'moves_pct',
      ticker: null,
      watchlist_id: 'wl_1',
      level: null,
      pct: 8,
      window_days: 5,
      name: 'Tech swings',
    });
    req.flush(alert({ condition: 'moves_pct', pct: 8, window_days: 5 }));
    await tick();
  });

  it('checks the percent and the days', () => {
    pick('Fires when the price', 'Moves by');
    type('#pa-ticker', 'AAA.US');
    type('#pa-pct', '0');
    type('#pa-days', '1.5');
    submit();
    fixture.detectChanges();
    expect(el().querySelector('#pa-pct-error')).not.toBeNull();
    expect(el().querySelector('#pa-days-error')?.textContent).toContain('whole days');
  });

  it('changes only the thresholds of an alert', async () => {
    fixture.componentRef.setInput('editing', alert({ name: 'Breakout' }));
    fixture.detectChanges();
    expect(el().querySelector('#pa-ticker')).toBeNull();
    expect(el().querySelector('.fixed')?.textContent).toContain('AAA.US');
    expect(el().querySelector('.fixed')?.textContent).toContain('rises above');
    expect(el().querySelector<HTMLInputElement>('#pa-level')!.value).toBe('250');
    type('#pa-level', '260');
    submit();
    const req = await nextRequest(http, '/api/price-alerts/pa_1', 'PATCH');
    expect(req.request.body).toEqual({
      level: 260,
      pct: null,
      window_days: null,
      name: 'Breakout',
    });
    req.flush(alert({ level: 260 }));
    await tick();
  });
});

describe('price alert words', () => {
  it('reads a rule in plain words', () => {
    expect(conditionText(alert())).toBe('Rises above 250');
    expect(conditionText(alert({ condition: 'moves_pct', pct: 8, window_days: 1 }))).toBe(
      'Moves 8% either way in 1 day',
    );
    expect(targetText(alert({ target_kind: 'watchlist', watchlist_id: 'wl_1' }), [TECH])).toBe(
      'Every ticker in Tech',
    );
    expect(targetText(alert({ target_kind: 'watchlist', watchlist_id: 'gone' }), [])).toBe(
      'Every ticker in a watchlist',
    );
  });
});
