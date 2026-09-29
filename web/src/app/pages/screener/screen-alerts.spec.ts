import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { SavedScreenView, ScreenAlertView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { SAVED } from '../../../testing/screener-fixtures';
import { ScreenAlerts, alertBody, cadenceLabel } from './screen-alerts';

const ALERT: ScreenAlertView = {
  screen_id: SAVED.id,
  screen_name: SAVED.name,
  enabled: true,
  cadence: 'weekly',
  weekday: 4,
  last_as_of: '2026-09-25',
  last_error: null,
  matched: 7,
  created_at: '2026-09-20T10:00:00Z',
  updated_at: '2026-09-25T22:00:00Z',
};

@Component({
  imports: [ScreenAlerts],
  template: `<app-screen-alerts [screens]="screens()" />`,
})
class Host {
  readonly screens = signal<readonly SavedScreenView[]>([SAVED]);
}

describe('cadenceLabel and alertBody', () => {
  it('reads and writes the cadence', () => {
    expect(cadenceLabel({ cadence: 'daily', weekday: null })).toBe('Daily');
    expect(cadenceLabel({ cadence: 'weekly', weekday: 4 })).toBe('Weekly on Friday');
    expect(alertBody('daily')).toEqual({ enabled: true, cadence: 'daily', weekday: null });
    expect(alertBody('2')).toEqual({ enabled: true, cadence: 'weekly', weekday: 2 });
  });
});

describe('ScreenAlerts', () => {
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
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  async function render(alerts: ScreenAlertView[] = [ALERT]) {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    (await nextRequest(http, '/api/screener/alerts')).flush(page(alerts));
    (await nextRequest(http, '/api/screener/alerts/events')).flush(
      page([
        {
          id: 1,
          screen_id: SAVED.id,
          screen_name: SAVED.name,
          as_of: '2026-09-25',
          tickers: ['AAA.US', 'BBB.US'],
          matched: 7,
          created_at: '2026-09-25T22:00:00Z',
        },
      ]),
    );
    await tick();
    fixture.detectChanges();
    return fixture;
  }

  it('shows each saved screen with its alert and the names found', async () => {
    const fixture = await render();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('Weekly on Friday');
    expect(root.textContent).toContain('7 matched');
    expect(root.textContent).toContain('AAA.US, BBB.US');
    const select = root.querySelector('select') as HTMLSelectElement;
    expect(select.labels?.[0]?.textContent).toContain(`When ${SAVED.name} alerts`);
  });

  it('turns an alert on from the When select', async () => {
    const fixture = await render([]);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('No alert.');
    const select = root.querySelector('select') as HTMLSelectElement;
    select.value = 'daily';
    select.dispatchEvent(new Event('change'));
    const put = await nextRequest(http, `/api/screener/screens/${SAVED.id}/alert`, 'PUT');
    expect(put.request.body).toEqual({ enabled: true, cadence: 'daily', weekday: null });
    put.flush({ ...ALERT, cadence: 'daily', weekday: null, last_as_of: null });
    (await nextRequest(http, '/api/screener/alerts')).flush(
      page([{ ...ALERT, cadence: 'daily', weekday: null, last_as_of: null }]),
    );
    await tick();
    fixture.detectChanges();
    expect(root.textContent).toContain('Not run yet.');
  });

  it('puts the select back to the stored schedule when a save fails', async () => {
    const fixture = await render();
    const root = fixture.nativeElement as HTMLElement;
    const select = root.querySelector('select') as HTMLSelectElement;
    const before = select.value;
    select.value = 'daily';
    select.dispatchEvent(new Event('change'));
    (await nextRequest(http, `/api/screener/screens/${SAVED.id}/alert`, 'PUT')).flush(
      { title: 'Server error', status: 500, detail: 'busy' },
      { status: 500, statusText: 'Server Error' },
    );
    await tick();
    fixture.detectChanges();
    expect(select.value).toBe(before);
  });

  it('removes an alert after asking', async () => {
    confirm.mockResolvedValue(true);
    const fixture = await render();
    const root = fixture.nativeElement as HTMLElement;
    const remove = root.querySelector(
      `button[aria-label="Remove the alert on ${SAVED.name}"]`,
    ) as HTMLButtonElement;
    remove.click();
    await tick();
    const del = await nextRequest(http, `/api/screener/screens/${SAVED.id}/alert`, 'DELETE');
    del.flush(null, { status: 204, statusText: 'No Content' });
    (await nextRequest(http, '/api/screener/alerts')).flush(page([]));
    await tick();
    fixture.detectChanges();
    expect(confirm).toHaveBeenCalledOnce();
    expect(root.textContent).toContain('No alert.');
  });
});
