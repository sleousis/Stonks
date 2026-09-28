import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { FeedItemView, FeedView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import {
  NOTIFICATION_POLL_MS,
  NotificationFeedService,
} from '../../core/notify/notification-feed.service';
import { nextRequest, tick } from '../../../testing/http';
import { FEED_PAGE } from './notification-feed';
import { NotificationsPage } from './notifications.page';

@Component({ template: '' })
class Blank {}

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

const FILL: FeedItemView = {
  id: 12,
  category: 'order',
  level: 'info',
  title: 'Bought 10 AAPL.US',
  message: 'Filled at the open.',
  deep_link: '/orders',
  created_at: minutesAgo(5),
  read_at: null,
};
const RISK: FeedItemView = {
  id: 11,
  category: 'risk',
  level: 'warning',
  title: 'Drawdown at 8%',
  message: 'Buys are paused for this portfolio.',
  deep_link: null,
  created_at: minutesAgo(120),
  read_at: minutesAgo(60),
};
const FEED: FeedView = { items: [FILL, RISK], unread_count: 1 };

describe('NotificationsPage', () => {
  let fixture: ComponentFixture<NotificationsPage>;
  let http: HttpTestingController;
  let allowed: boolean;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'orders', component: Blank },
          { path: 'settings', component: Blank },
        ]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: NOTIFICATION_POLL_MS, useValue: 0 },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      allowed ? null : 'Traders and admins only.',
    );
  });

  afterEach(() => http.verify());

  async function render(feed: FeedView = FEED) {
    fixture = TestBed.createComponent(NotificationsPage);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/notifications');
    req.flush(feed);
    await tick();
    fixture.detectChanges();
    return { el: fixture.nativeElement as HTMLElement, req };
  }

  async function settle() {
    await tick();
    fixture.detectChanges();
  }

  const items = (el: HTMLElement) => [...el.querySelectorAll<HTMLElement>('.feed li')];
  const button = (root: HTMLElement, text: string) =>
    [...root.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent?.includes(text),
    );

  it('lists the feed newest first and marks unread items with a dot and a word', async () => {
    const { el, req } = await render();
    expect(req.request.urlWithParams).toContain('unread_only=false');
    expect(req.request.urlWithParams).toContain(`limit=${FEED_PAGE}`);
    const [first, second] = items(el);
    expect(first.classList).toContain('unread');
    expect(first.querySelector('.unread-tag')?.textContent?.trim()).toBe('Unread');
    expect(first.textContent).toContain('Order');
    expect(first.querySelector('time')?.textContent?.trim()).toBe('5m ago');
    expect(second.classList).not.toContain('unread');
    expect(second.querySelector('.unread-tag')).toBeNull();
    expect(second.textContent).toContain('Risk');
    expect(second.querySelector('app-status-pill')?.textContent?.trim()).toBe('Warning');
    expect(el.querySelector('#feed-title')?.textContent).toContain('1 unread');
    expect(TestBed.inject(NotificationFeedService).unread()).toBe(1);
  });

  it('marks all read with an empty body', async () => {
    const { el } = await render();
    button(el, 'Mark all read')!.click();
    const req = await nextRequest(http, '/api/notifications/read', 'POST');
    expect(req.request.body).toEqual({});
    req.flush({ updated: 1, unread_count: 0 });
    await settle();
    expect(el.querySelectorAll('.unread-tag').length).toBe(0);
    expect(TestBed.inject(NotificationFeedService).unread()).toBe(0);
    expect(button(el, 'Mark all read')!.disabled).toBe(true);
  });

  it('marks one item read by its id', async () => {
    const { el } = await render();
    button(items(el)[0], 'Mark read')!.click();
    const req = await nextRequest(http, '/api/notifications/read', 'POST');
    expect(req.request.body).toEqual({ ids: [12] });
    req.flush({ updated: 1, unread_count: 0 });
    await settle();
    expect(items(el)[0].classList).not.toContain('unread');
  });

  it('opens a deep link in the app and marks the item read', async () => {
    const { el } = await render();
    const open = items(el)[0].querySelector<HTMLAnchorElement>('a.open')!;
    expect(open.getAttribute('href')).toBe('/orders');
    expect(items(el)[1].querySelector('a.open')).toBeNull();
    open.click();
    const req = await nextRequest(http, '/api/notifications/read', 'POST');
    expect(req.request.body).toEqual({ ids: [12] });
    req.flush({ updated: 1, unread_count: 0 });
    await settle();
    expect(TestBed.inject(Router).url).toBe('/orders');
  });

  it('filters to unread only', async () => {
    const { el } = await render();
    button(el, 'Unread only')!.click();
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/notifications');
    expect(req.request.urlWithParams).toContain('unread_only=true');
    req.flush({ items: [], unread_count: 0 });
    await settle();
    expect(el.textContent).toContain('Nothing unread');
  });

  it('explains what lands here when the feed is empty', async () => {
    const { el } = await render({ items: [], unread_count: 0 });
    expect(el.textContent).toContain('No notifications yet');
    expect(el.textContent).toContain('order fills');
  });

  it('pages to older items with before_id', async () => {
    const page = Array.from({ length: FEED_PAGE }, (_, i) => ({
      ...RISK,
      id: 100 - i,
      title: `Item ${100 - i}`,
    }));
    const { el } = await render({ items: page, unread_count: 0 });
    button(el, 'Show older')!.click();
    const req = await nextRequest(http, '/api/notifications');
    expect(req.request.urlWithParams).toContain(`before_id=${100 - FEED_PAGE + 1}`);
    req.flush({ items: [{ ...RISK, id: 5, title: 'Oldest' }], unread_count: 0 });
    await settle();
    expect(items(el).length).toBe(FEED_PAGE + 1);
    expect(el.textContent).toContain('Oldest');
    expect(button(el, 'Show older')).toBeUndefined();
  });

  it('drops an older page that arrives after the filter changed (UX-33)', async () => {
    const page = Array.from({ length: FEED_PAGE }, (_, i) => ({
      ...RISK,
      id: 100 - i,
      title: `Item ${100 - i}`,
    }));
    const { el } = await render({ items: page, unread_count: 0 });
    button(el, 'Show older')!.click();
    const older = await nextRequest(http, '/api/notifications');
    expect(older.request.urlWithParams).toContain('before_id=');

    button(el, 'Unread only')!.click();
    fixture.detectChanges();
    const unread = await nextRequest(http, '/api/notifications');
    expect(unread.request.urlWithParams).toContain('unread_only=true');
    unread.flush({ items: [{ ...RISK, id: 200, title: 'Fresh unread' }], unread_count: 1 });
    await settle();

    older.flush({ items: [{ ...RISK, id: 5, title: 'Stale older' }], unread_count: 0 });
    await settle();
    expect(el.textContent).toContain('Fresh unread');
    expect(el.textContent).not.toContain('Stale older');
    expect(items(el).length).toBe(1);
  });

  it('shows the filter as a radio group', async () => {
    const { el } = await render();
    const radios = [...el.querySelectorAll('[role=radiogroup] [role=radio]')];
    expect(radios.map((r) => r.textContent?.trim())).toEqual(['All', 'Unread only']);
    expect(radios[0].getAttribute('aria-checked')).toBe('true');
  });

  it('locks mark-read for a user who may not manage notifications', async () => {
    allowed = false;
    const { el } = await render();
    expect(button(el, 'Mark all read')!.disabled).toBe(true);
    expect(button(items(el)[0], 'Mark read')!.disabled).toBe(true);
    expect(el.textContent).toContain('Traders and admins only.');
  });
});
