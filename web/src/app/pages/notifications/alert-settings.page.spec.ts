import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PreferencesView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { DEVICE_INFO, NOTIFICATION_API } from '../../core/pwa/notification-permission.service';
import { page, tick } from '../../../testing/http';
import { provideFakeCalendars } from '../../../testing/fake-calendars';
import { AlertSettingsPage } from './alert-settings.page';

const VIEW: PreferencesView = {
  channels: ['inapp', 'webpush', 'webhook'],
  preferences: [],
  quiet_start: null,
  quiet_end: null,
  timezone: 'UTC',
  webhook: null,
  event_alerts: [{ topic: 'earnings', label: 'Earnings coming up', enabled: true }],
};

describe('AlertSettingsPage', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeCalendars(),
        { provide: NOTIFICATION_API, useValue: null },
        { provide: DEVICE_INFO, useValue: { ios: false, standalone: false, userAgent: 'test' } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
  });

  afterEach(() => http.verify());

  /** Answer every read the page makes: two preference panels, devices, Telegram. */
  async function render(): Promise<HTMLElement> {
    const fixture = TestBed.createComponent(AlertSettingsPage);
    fixture.detectChanges();
    for (let i = 0; i < 30; i++) {
      await tick(1);
      for (const req of http.match((r) =>
        r.url.split('?')[0].endsWith('/api/notifications/preferences'),
      )) {
        req.flush(VIEW);
      }
      for (const req of http.match((r) =>
        r.url.split('?')[0].endsWith('/api/push/subscriptions'),
      )) {
        req.flush(page([]));
      }
      for (const req of http.match((r) => r.url.split('?')[0].endsWith('/api/telegram/link'))) {
        req.flush({ bot_configured: false, bot_enabled: false, linked: false });
      }
      for (const req of http.match((r) =>
        r.url.split('?')[0].endsWith('/api/assistant/briefings/prefs'),
      )) {
        req.flush({ available: false, pre_open: false, post_close: false });
      }
    }
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('puts every alert setting on one page, in two plain groups (F39)', async () => {
    const el = await render();
    const groups = [...el.querySelectorAll('h2')].map((h) => h.textContent?.trim());
    expect(groups).toEqual(['Where alerts reach you', 'What reaches you, and when']);
    const panels = [...el.querySelectorAll('h3')].map((h) => h.textContent?.trim());
    expect(panels).toEqual([
      'Push on this device',
      'Your devices',
      'Telegram',
      'Your webhook',
      'Which alerts go where',
      'Upcoming events',
      'Quiet hours',
      'Briefings',
      'Price alerts',
    ]);
  });

  it('says price alerts check daily closes and links to them', async () => {
    const el = await render();
    expect(el.textContent).toContain("each day's closing price");
    const link = el.querySelector<HTMLAnchorElement>('.btn[href="/notifications/price-alerts"]');
    expect(link?.textContent?.trim()).toBe('Open price alerts');
  });

  it('shows the three Notifications tabs', async () => {
    const el = await render();
    const tabs = [...el.querySelectorAll('.tabs a')].map((a) => a.textContent?.trim());
    expect(tabs).toEqual(['Feed', 'Price alerts', 'Alert settings']);
  });
});
