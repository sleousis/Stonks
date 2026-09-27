import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PreferencesView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { NotificationPrefs } from './notification-prefs';

const VIEW: PreferencesView = {
  channels: ['inapp', 'webpush', 'webhook'],
  preferences: [{ category: 'order', channel: 'webpush', enabled: false, strategy_id: null }],
  quiet_start: null,
  quiet_end: null,
  timezone: 'Europe/London',
  webhook: null,
  event_alerts: [
    { topic: 'earnings', label: 'Earnings coming up', enabled: true },
    { topic: 'dividends', label: 'Ex-dividend dates coming up', enabled: false },
    { topic: 'economic', label: 'Economic releases coming up', enabled: true },
  ],
};

describe('NotificationPrefs', () => {
  let fixture: ComponentFixture<NotificationPrefs>;
  let controller: HttpTestingController;
  let allowed: boolean;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      allowed ? null : 'Traders and admins only.',
    );
  });

  function button(el: HTMLElement, text: string) {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.includes(text));
  }

  function type(el: HTMLElement, id: string, value: string) {
    const input = el.querySelector<HTMLInputElement>(id)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  afterEach(() => controller.verify());

  async function render(view = VIEW) {
    fixture = TestBed.createComponent(NotificationPrefs);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/notifications/preferences')).flush(view);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function box(el: HTMLElement, label: string) {
    return [...el.querySelectorAll('label.cell')]
      .find((l) => l.textContent?.includes(label))!
      .querySelector('input')!;
  }

  it('shows a switch per alert type and channel, without the in-app feed', async () => {
    const el = await render();
    const heads = [...el.querySelectorAll('thead th')].map((th) => th.textContent?.trim());
    expect(heads).toEqual(['Alert', 'Push', 'Webhook']);
    expect(box(el, 'Signals by Push').checked).toBe(true);
    expect(box(el, 'Orders and fills by Push').checked).toBe(false);
    expect(el.textContent).toContain('Europe/London');
  });

  it('saves one switch at a time', async () => {
    const el = await render();
    box(el, 'Signals by Push').click();
    const req = await nextRequest(controller, '/api/notifications/preferences', 'PUT');
    expect(req.request.body).toEqual({
      preferences: [{ category: 'signal', channel: 'webpush', enabled: false }],
    });
    req.flush({
      ...VIEW,
      preferences: [
        ...VIEW.preferences,
        { category: 'signal', channel: 'webpush', enabled: false, strategy_id: null },
      ],
    });
    await tick();
    fixture.detectChanges();
    expect(box(el, 'Signals by Push').checked).toBe(false);
  });

  it('gives price alerts and upcoming events their own rows, apart from signals', async () => {
    const el = await render();
    const rows = [...el.querySelectorAll('tbody th')].map((th) => th.textContent?.trim());
    expect(rows).toEqual([
      'Signals',
      'Price alerts',
      'Upcoming events',
      'Orders and fills',
      'Risk alerts',
      'System',
    ]);
    box(el, 'Price alerts by Push').click();
    const req = await nextRequest(controller, '/api/notifications/preferences', 'PUT');
    expect(req.request.body).toEqual({
      preferences: [{ category: 'price_alert', channel: 'webpush', enabled: false }],
    });
    req.flush(VIEW);
    await tick();
  });

  function kind(el: HTMLElement, label: string) {
    return [...el.querySelectorAll('label.kind')]
      .find((l) => l.textContent?.includes(label))!
      .querySelector('input')!;
  }

  it('shows one switch per kind of upcoming event and saves it', async () => {
    const el = await render();
    expect(kind(el, 'Earnings coming up').checked).toBe(true);
    expect(kind(el, 'Ex-dividend dates coming up').checked).toBe(false);
    expect(kind(el, 'Economic releases coming up').checked).toBe(true);
    kind(el, 'Earnings coming up').click();
    const req = await nextRequest(controller, '/api/notifications/preferences', 'PUT');
    expect(req.request.body).toEqual({ event_alerts: [{ topic: 'earnings', enabled: false }] });
    req.flush({
      ...VIEW,
      event_alerts: VIEW.event_alerts!.map((e) =>
        e.topic === 'earnings' ? { ...e, enabled: false } : e,
      ),
    });
    await tick();
    fixture.detectChanges();
    expect(kind(el, 'Earnings coming up').checked).toBe(false);
  });

  it('sets and clears quiet hours', async () => {
    const el = await render();
    const set = (id: string, v: string) => {
      const input = el.querySelector<HTMLInputElement>(id)!;
      input.value = v;
      input.dispatchEvent(new Event('input'));
    };
    set('#quiet-start', '22:00');
    set('#quiet-end', '07:00');
    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Save quiet'))!.click();
    const req = await nextRequest(controller, '/api/notifications/quiet-hours', 'PUT');
    expect(req.request.body).toEqual({ start: '22:00', end: '07:00' });
    req.flush({ ...VIEW, quiet_start: '22:00', quiet_end: '07:00' });
    await tick();
    fixture.detectChanges();

    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Turn off'))!.click();
    const clear = await nextRequest(controller, '/api/notifications/quiet-hours', 'PUT');
    expect(clear.request.body).toEqual({ start: null, end: null });
    clear.flush(VIEW);
    await tick();
  });

  it('asks for both times', async () => {
    const el = await render();
    const start = el.querySelector<HTMLInputElement>('#quiet-start')!;
    start.value = '22:00';
    start.dispatchEvent(new Event('input'));
    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Save quiet'))!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Pick both times');
  });

  it('says when the server has no delivery channels', async () => {
    const el = await render({ ...VIEW, channels: ['inapp'] });
    expect(el.textContent).toContain('no push, email or webhook delivery');
  });

  it('saves the webhook write-only and shows only its host afterwards', async () => {
    const el = await render();
    expect(el.textContent).toContain('No webhook set');
    type(el, '#webhook-url', 'https://hooks.example.com/secret-path');
    button(el, 'Save webhook')!.click();
    const req = await nextRequest(controller, '/api/notifications/webhook', 'PUT');
    expect(req.request.body).toEqual({ url: 'https://hooks.example.com/secret-path' });
    req.flush({ ...VIEW, webhook: 'https://hooks.example.com/***' });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Sending to https://hooks.example.com/***');
    expect(el.querySelector<HTMLInputElement>('#webhook-url')!.value).toBe('');
    expect(el.textContent).not.toContain('secret-path');
  });

  it('asks for an https address', async () => {
    const el = await render();
    type(el, '#webhook-url', 'http://hooks.example.com');
    button(el, 'Save webhook')!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('starts with https://');
  });

  it('asks before removing the webhook', async () => {
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = await render({ ...VIEW, webhook: 'https://hooks.example.com/***' });
    button(el, 'Remove webhook')!.click();
    const req = await nextRequest(controller, '/api/notifications/webhook', 'PUT');
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ tone: 'danger' }));
    expect(req.request.body).toEqual({ url: null });
    req.flush(VIEW);
    await tick();
  });

  it('hides the webhook when the server cannot send to one', async () => {
    const el = await render({ ...VIEW, channels: ['inapp', 'webpush'] });
    expect(el.querySelector('#webhook-url')).toBeNull();
  });

  it('locks every setting for a user who may not change them', async () => {
    allowed = false;
    const el = await render();
    expect(el.textContent).toContain('Traders and admins only.');
    expect(box(el, 'Signals by Push').disabled).toBe(true);
    expect(kind(el, 'Earnings coming up').disabled).toBe(true);
    expect(button(el, 'Save quiet hours')!.disabled).toBe(true);
    expect(button(el, 'Save webhook')!.disabled).toBe(true);
    expect(el.querySelector<HTMLInputElement>('#webhook-url')!.disabled).toBe(true);
  });

  it('sends a test notification and says where it went', async () => {
    const toasts = TestBed.inject(ToastService);
    const success = vi.spyOn(toasts, 'success');
    const info = vi.spyOn(toasts, 'info');
    const el = await render();
    button(el, 'Send a test notification')!.click();
    (await nextRequest(controller, '/api/notifications/test', 'POST')).flush({
      notification_id: 7,
      deliveries: 2,
      channels: ['inapp', 'webpush', 'webhook'],
    });
    await tick();
    expect(success).toHaveBeenCalledWith(expect.stringContaining('2 deliveries by push, webhook'));
    fixture.detectChanges();
    button(el, 'Send a test notification')!.click();
    (await nextRequest(controller, '/api/notifications/test', 'POST')).flush({
      notification_id: 8,
      deliveries: 0,
      channels: ['inapp'],
    });
    await tick();
    expect(info).toHaveBeenCalledWith(expect.stringContaining('Turn on push'));
  });

  it('asks for a moment when a test was sent in the last minute (429)', async () => {
    const toasts = TestBed.inject(ToastService);
    const info = vi.spyOn(toasts, 'info');
    const error = vi.spyOn(toasts, 'error');
    const el = await render();
    button(el, 'Send a test notification')!.click();
    (await nextRequest(controller, '/api/notifications/test', 'POST')).flush(
      { title: 'Too Many Requests', status: 429, detail: 'One test notification a minute.' },
      { status: 429, statusText: 'Too Many Requests', headers: { 'Retry-After': '42' } },
    );
    await tick();
    expect(info).toHaveBeenCalledWith('One test a minute. Wait a moment, then send another.');
    expect(error).not.toHaveBeenCalled();
    fixture.detectChanges();
    expect(button(el, 'Send a test notification')!.disabled).toBe(false);
  });
});
