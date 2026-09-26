import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DestroyRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import {
  NOTIFICATION_POLL_MS,
  NotificationFeedService,
  appLink,
} from './notification-feed.service';
import { ToastService } from './toast.service';

const FEED = '/api/notifications';

function fakeDestroyRef() {
  const callbacks: (() => void)[] = [];
  const ref = { onDestroy: (c: () => void) => callbacks.push(c) } as unknown as DestroyRef;
  return { ref, destroy: () => callbacks.forEach((c) => c()) };
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('NotificationFeedService', () => {
  let http: HttpTestingController;
  let feed: NotificationFeedService;

  function setup(pollMs: number) {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: NOTIFICATION_POLL_MS, useValue: pollMs },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    feed = TestBed.inject(NotificationFeedService);
  }

  /** Flush every pending feed read with this count. */
  function flushAll(unread: number) {
    for (const req of http.match((r) => r.url === FEED)) {
      req.flush({ items: [], unread_count: unread });
    }
  }

  beforeEach(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    });
  });

  it('reads the unread count quietly and polls while the tab is visible', async () => {
    setup(5);
    const { ref, destroy } = fakeDestroyRef();
    feed.watch(ref);
    const first = await nextRequest(http, FEED);
    expect(first.request.urlWithParams).toContain('limit=1');
    first.flush({ items: [], unread_count: 3 });
    await tick();
    expect(feed.unread()).toBe(3);

    (await nextRequest(http, FEED)).flush({ items: [], unread_count: 4 });
    await tick();
    expect(feed.unread()).toBe(4);
    destroy();
    flushAll(4);
  });

  it('pauses while the tab is hidden and reads again on return', async () => {
    setup(5);
    const { ref, destroy } = fakeDestroyRef();
    feed.watch(ref);
    (await nextRequest(http, FEED)).flush({ items: [], unread_count: 1 });
    setVisibility('hidden');
    flushAll(1);
    await tick(30);
    expect(http.match((r) => r.url === FEED).length).toBe(0);

    setVisibility('visible');
    (await nextRequest(http, FEED)).flush({ items: [], unread_count: 2 });
    await tick();
    expect(feed.unread()).toBe(2);
    destroy();
    flushAll(2);
  });

  it('keeps the last count when a read fails, without a toast', async () => {
    setup(0);
    const { ref, destroy } = fakeDestroyRef();
    feed.watch(ref);
    (await nextRequest(http, FEED)).flush({ items: [], unread_count: 5 });
    await tick();
    const again = feed.refresh();
    (await nextRequest(http, FEED)).flush(null, { status: 500, statusText: 'Server Error' });
    await again;
    expect(feed.unread()).toBe(5);
    expect(TestBed.inject(ToastService).toasts()).toEqual([]);
    destroy();
  });

  it('shares one poll between watchers and stops when the last goes', async () => {
    setup(5);
    const a = fakeDestroyRef();
    const b = fakeDestroyRef();
    feed.watch(a.ref);
    feed.watch(b.ref);
    (await nextRequest(http, FEED)).flush({ items: [], unread_count: 0 });
    a.destroy();
    (await nextRequest(http, FEED)).flush({ items: [], unread_count: 0 });
    b.destroy();
    flushAll(0);
    await tick(30);
    expect(http.match((r) => r.url === FEED).length).toBe(0);
  });

  it('takes the count from a mark-read reply', async () => {
    setup(0);
    const done = feed.markRead([7]);
    const req = await nextRequest(http, '/api/notifications/read', 'POST');
    expect(req.request.body).toEqual({ ids: [7] });
    req.flush({ updated: 1, unread_count: 2 });
    await done;
    expect(feed.unread()).toBe(2);
  });

  it('opens only same-app deep links', () => {
    expect(appLink('/orders')).toBe('/orders');
    expect(appLink('//evil.example')).toBeNull();
    expect(appLink('https://x.example')).toBeNull();
    expect(appLink(null)).toBeNull();
  });
});
