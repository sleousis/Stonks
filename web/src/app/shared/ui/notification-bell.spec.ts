import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { NOTIFICATION_POLL_MS } from '../../core/notify/notification-feed.service';
import { nextRequest, tick } from '../../../testing/http';
import { NotificationBell } from './notification-bell';

describe('NotificationBell', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: NOTIFICATION_POLL_MS, useValue: 0 },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(unread: number) {
    const fixture = TestBed.createComponent(NotificationBell);
    fixture.detectChanges();
    (await nextRequest(http, '/api/notifications')).flush({ items: [], unread_count: unread });
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('links to the feed and says how many are unread', async () => {
    const el = await render(3);
    const link = el.querySelector('a')!;
    expect(link.getAttribute('href')).toBe('/notifications');
    expect(link.getAttribute('aria-label')).toBe('Notifications, 3 unread');
    expect(el.querySelector('.badge')?.textContent?.trim()).toBe('3');
  });

  it('hides the badge when nothing is unread', async () => {
    const el = await render(0);
    expect(el.querySelector('a')!.getAttribute('aria-label')).toBe('Notifications');
    expect(el.querySelector('.badge')).toBeNull();
  });

  it('caps the badge at 99+', async () => {
    const el = await render(250);
    expect(el.querySelector('.badge')?.textContent?.trim()).toBe('99+');
  });
});
