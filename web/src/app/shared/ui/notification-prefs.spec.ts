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
import { provideFakeCalendars } from '../../../testing/fake-calendars';

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
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeCalendars(),
      ],
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

  it('shows a channel the server keeps off by default as off until it is turned on', async () => {
    const el = await render({
      ...VIEW,
      channels: ['telegram', 'webhook', 'webpush'],
      preferences: [],
      channel_defaults: [
        { channel: 'telegram', default_enabled: true, fallback: true },
        { channel: 'webhook', default_enabled: false, fallback: true },
        { channel: 'webpush', default_enabled: true, fallback: false },
      ],
    });
    expect(box(el, 'Signals by Telegram').checked).toBe(true);
    expect(box(el, 'Signals by Push').checked).toBe(true);
    expect(box(el, 'Signals by Webhook').checked).toBe(false);
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
    const rows = [...el.querySelectorAll('tbody th')].map((th) =>
      th.firstChild?.textContent?.trim(),
    );
    expect(rows).toEqual([
      'Signals',
      'Price alerts',
      'Upcoming events',
      'Screen alerts',
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

  it("never offers the server's own log as a channel (M6)", async () => {
    const el = await render({ ...VIEW, channels: ['inapp', 'log', 'webpush'] });
    const heads = [...el.querySelectorAll('thead th')].map((th) => th.textContent?.trim());
    expect(heads).toEqual(['Alert', 'Push']);
  });

  it('says in the Price alerts row that they check daily closes', async () => {
    const el = await render();
    const row = [...el.querySelectorAll('tbody th')].find((th) =>
      th.textContent?.includes('Price alerts'),
    )!;
    expect(row.querySelector('.row-hint')?.textContent).toContain('daily closes');
  });

  it('shows one titled panel per part, or only the parts asked for', async () => {
    const el = await render();
    const titles = [...el.querySelectorAll('h3')].map((h) => h.textContent?.trim());
    expect(titles).toEqual([
      'Which alerts go where',
      'Upcoming events',
      'Quiet hours',
      'Your webhook',
    ]);
    fixture.componentRef.setInput('only', ['webhook']);
    fixture.detectChanges();
    expect([...el.querySelectorAll('h3')].map((h) => h.textContent?.trim())).toEqual([
      'Your webhook',
    ]);
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

  const ECON: PreferencesView = {
    ...VIEW,
    economic_alerts: {
      countries: ['US', 'EU'],
      default_countries: true,
      min_importance: 'high',
      country_options: [
        { value: 'US', label: 'United States' },
        { value: 'EU', label: 'Euro area' },
        { value: 'GB', label: 'United Kingdom' },
      ],
      importance_options: [
        { value: 'low', label: 'All releases' },
        { value: 'medium', label: 'Medium and high importance' },
        { value: 'high', label: 'High importance only' },
      ],
    },
  };

  it('hides the economic release choices when the server sends none', async () => {
    const el = await render();
    expect(el.querySelector('#econ-importance')).toBeNull();
  });

  it('shows the economic countries and importance, defaults noted', async () => {
    const el = await render(ECON);
    expect(el.textContent).toContain('follow the currencies of your portfolios');
    expect(kind(el, 'United States').checked).toBe(true);
    expect(kind(el, 'Euro area').checked).toBe(true);
    expect(kind(el, 'United Kingdom').checked).toBe(false);
    const select = el.querySelector<HTMLSelectElement>('#econ-importance')!;
    expect(select.value).toBe('high');
    expect(button(el, 'Follow my portfolio currencies')).toBeUndefined();
  });

  it('saves a country added to the economic alerts', async () => {
    const el = await render(ECON);
    kind(el, 'United Kingdom').click();
    const req = await nextRequest(controller, '/api/notifications/preferences', 'PUT');
    expect(req.request.body).toEqual({ economic_alerts: { countries: ['US', 'EU', 'GB'] } });
    req.flush({
      ...ECON,
      economic_alerts: {
        ...ECON.economic_alerts!,
        countries: ['US', 'EU', 'GB'],
        default_countries: false,
      },
    });
    await tick();
    fixture.detectChanges();
    expect(kind(el, 'United Kingdom').checked).toBe(true);
    expect(button(el, 'Follow my portfolio currencies')).toBeDefined();
  });

  it('keeps at least one country', async () => {
    const one: PreferencesView = {
      ...ECON,
      economic_alerts: { ...ECON.economic_alerts!, countries: ['US'] },
    };
    const el = await render(one);
    kind(el, 'United States').click();
    fixture.detectChanges();
    expect(kind(el, 'United States').checked).toBe(true);
    expect(el.textContent).toContain('Keep at least one country');
  });

  it('saves the importance threshold', async () => {
    const el = await render(ECON);
    const select = el.querySelector<HTMLSelectElement>('#econ-importance')!;
    select.value = 'medium';
    select.dispatchEvent(new Event('change'));
    const req = await nextRequest(controller, '/api/notifications/preferences', 'PUT');
    expect(req.request.body).toEqual({ economic_alerts: { min_importance: 'medium' } });
    req.flush({ ...ECON, economic_alerts: { ...ECON.economic_alerts!, min_importance: 'medium' } });
    await tick();
  });

  it('goes back to the portfolio currencies', async () => {
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    const el = await render({
      ...ECON,
      economic_alerts: { ...ECON.economic_alerts!, countries: ['GB'], default_countries: false },
    });
    button(el, 'Follow my portfolio currencies')!.click();
    const req = await nextRequest(controller, '/api/notifications/preferences', 'PUT');
    expect(req.request.body).toEqual({ economic_alerts: { default_countries: true } });
    req.flush(ECON);
    await tick();
    expect(success).toHaveBeenCalledWith(expect.stringContaining('portfolio currencies'));
  });

  it('notes when economic releases are turned off', async () => {
    const el = await render({
      ...ECON,
      event_alerts: VIEW.event_alerts!.map((e) =>
        e.topic === 'economic' ? { ...e, enabled: false } : e,
      ),
    });
    expect(el.textContent).toContain('Turn on Economic releases coming up to get them');
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

  it('keeps quiet hours typed but not saved when another switch is saved', async () => {
    const el = await render();
    const set = (id: string, v: string) => {
      const input = el.querySelector<HTMLInputElement>(id)!;
      input.value = v;
      input.dispatchEvent(new Event('input'));
    };
    set('#quiet-start', '22:00');
    set('#quiet-end', '07:00');
    box(el, 'Signals by Push').click();
    (await nextRequest(controller, '/api/notifications/preferences', 'PUT')).flush({ ...VIEW });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector<HTMLInputElement>('#quiet-start')!.value).toBe('22:00');
    expect(el.querySelector<HTMLInputElement>('#quiet-end')!.value).toBe('07:00');
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
