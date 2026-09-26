import type { Mock } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { SwPush } from '@angular/service-worker';
import { of } from 'rxjs';

import type { PushDeviceView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { NotificationPermissionService } from '../../core/pwa/notification-permission.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { PushDevices, deviceName, isThisBrowser } from './push-devices';

const PHONE: PushDeviceView = {
  id: 'dev_1',
  endpoint_host: 'web.push.apple.com',
  user_agent:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  created_at: '2026-09-01T10:00:00Z',
  last_success_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
  failure_count: 0,
};
const LAPTOP: PushDeviceView = {
  id: 'dev_2',
  endpoint_host: 'fcm.googleapis.com',
  user_agent: navigator.userAgent,
  created_at: '2026-09-10T10:00:00Z',
  last_success_at: null,
  failure_count: 2,
};

describe('PushDevices', () => {
  let fixture: ComponentFixture<PushDevices>;
  let http: HttpTestingController;
  let confirm: Mock<(o: ConfirmOptions) => Promise<boolean>>;
  const disable = vi.fn(async () => undefined);
  const permissionError = signal<string | null>(null);

  function setup(swPush?: object) {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: NotificationPermissionService, useValue: { disable, error: permissionError } },
        ...(swPush ? [{ provide: SwPush, useValue: swPush }] : []),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
    confirm = vi.fn<(o: ConfirmOptions) => Promise<boolean>>(async () => true);
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockImplementation(confirm);
  }

  afterEach(() => http.verify());

  async function render(devices: PushDeviceView[] = [PHONE, LAPTOP]) {
    fixture = TestBed.createComponent(PushDevices);
    fixture.detectChanges();
    (await nextRequest(http, '/api/push/subscriptions')).flush(page(devices));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const rows = (el: HTMLElement) => [...el.querySelectorAll<HTMLElement>('.devices li')];
  const removeIn = (row: HTMLElement) => row.querySelector<HTMLButtonElement>('button')!;

  it('names each device and says when it was added and last used', async () => {
    setup();
    const el = await render();
    const [phone, laptop] = rows(el);
    expect(phone.querySelector('.name')?.textContent).toContain('Safari on iPhone');
    expect(phone.textContent).toContain('Added 2026-09-01');
    expect(phone.textContent).toContain('Last used 2h ago');
    expect(laptop.textContent).toContain('Not used yet');
    expect(laptop.textContent).toContain('Recent deliveries failed');
    expect(el.querySelector('a[href="/settings"]')).not.toBeNull();
  });

  it('asks first, then removes another device', async () => {
    setup();
    const el = await render();
    removeIn(rows(el)[0]).click();
    const req = await nextRequest(http, '/api/push/subscriptions/dev_1', 'DELETE');
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Remove Safari on iPhone?', tone: 'danger' }),
    );
    req.flush(null, { status: 204, statusText: 'No Content' });
    (await nextRequest(http, '/api/push/subscriptions')).flush(page([LAPTOP]));
    await tick();
    fixture.detectChanges();
    expect(rows(el).length).toBe(1);
  });

  it('does nothing when the trader cancels', async () => {
    setup();
    const el = await render();
    confirm.mockResolvedValueOnce(false);
    removeIn(rows(el)[0]).click();
    await tick(5);
    expect(http.match(() => true).length).toBe(0);
  });

  it('shows the error when another device cannot be removed', async () => {
    setup();
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    const el = await render();
    removeIn(rows(el)[0]).click();
    (await nextRequest(http, '/api/push/subscriptions/dev_1', 'DELETE')).flush(
      { title: 'Not Found', status: 404, detail: 'push subscription not found' },
      { status: 404, statusText: 'Not Found' },
    );
    await tick();
    expect(error).toHaveBeenCalledWith(expect.stringContaining('not found'));
  });

  it('removes this browser by turning its push subscription off', async () => {
    setup({ isEnabled: true, subscription: of({ endpoint: 'https://fcm.googleapis.com/abc' }) });
    const el = await render();
    await tick();
    fixture.detectChanges();
    const laptop = rows(el)[1];
    expect(laptop.textContent).toContain('This browser');
    removeIn(laptop).click();
    (await nextRequest(http, '/api/push/subscriptions')).flush(page([PHONE]));
    expect(disable).toHaveBeenCalled();
    await tick();
  });

  it('points to Settings when there are no devices', async () => {
    setup();
    const el = await render([]);
    expect(el.textContent).toContain('No devices yet');
  });
});

describe('deviceName', () => {
  it('summarises a user agent', () => {
    expect(
      deviceName(
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 Edg/140.0',
      ),
    ).toBe('Edge on Windows');
    expect(
      deviceName('Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140.0 Mobile Safari'),
    ).toBe('Chrome on Android');
    expect(deviceName(null)).toBeNull();
  });

  it('matches this browser by push host and user agent', () => {
    const d = { endpoint_host: 'fcm.googleapis.com', user_agent: 'UA' };
    expect(isThisBrowser(d, 'https://fcm.googleapis.com/x', 'UA')).toBe(true);
    expect(isThisBrowser(d, 'https://fcm.googleapis.com/x', 'Other')).toBe(false);
    expect(isThisBrowser(d, null, 'UA')).toBe(false);
  });
});
