import { TestBed } from '@angular/core/testing';

import {
  DEVICE_INFO,
  NOTIFICATION_API,
  type NotificationApi,
} from '../../core/pwa/notification-permission.service';
import { NotificationSettings } from './notification-settings';

function render(notification: NotificationApi | null, ios = false) {
  TestBed.configureTestingModule({
    providers: [
      { provide: NOTIFICATION_API, useValue: notification },
      { provide: DEVICE_INFO, useValue: { ios, standalone: false, userAgent: 'test' } },
    ],
  });
  const fixture = TestBed.createComponent(NotificationSettings);
  fixture.detectChanges();
  return { fixture, el: fixture.nativeElement as HTMLElement };
}

describe('NotificationSettings', () => {
  it('offers a clear opt-in button and asks only when pressed', async () => {
    const notification = {
      permission: 'default' as NotificationPermission,
      requestPermission: vi.fn(async () => 'granted' as NotificationPermission),
    };
    const { fixture, el } = render(notification);
    expect(notification.requestPermission).not.toHaveBeenCalled();
    const button = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Turn on notifications'),
    )!;
    button.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(notification.requestPermission).toHaveBeenCalledOnce();
    // No service worker in tests: allowed, but delivery needs the installed console.
    expect(el.textContent).toContain('Allowed');
  });

  it('explains Add to Home Screen on iPhone', () => {
    const { el } = render(
      { permission: 'default', requestPermission: vi.fn() } as unknown as NotificationApi,
      true,
    );
    expect(el.textContent).toContain('Add to Home Screen');
    expect(el.querySelector('button')).toBeNull();
  });

  it('explains how to unblock', () => {
    const { el } = render({
      permission: 'denied',
      requestPermission: vi.fn(),
    } as unknown as NotificationApi);
    expect(el.textContent).toContain('blocked for this site');
  });

  it('says when the browser has no notifications', () => {
    const { el } = render(null);
    expect(el.textContent).toContain('cannot show notifications');
  });
});
