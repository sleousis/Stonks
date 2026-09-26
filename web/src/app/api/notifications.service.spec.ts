import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { NotificationsService } from './notifications.service';
import { provideApi } from './provide-api';

describe('NotificationsService', () => {
  let controller: HttpTestingController;
  let notifications: NotificationsService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    notifications = TestBed.inject(NotificationsService);
  });

  afterEach(() => controller.verify());

  it('reads the feed with its unread count', async () => {
    const feed = notifications.feed({ unread_only: true });
    const req = await nextRequest(controller, '/api/notifications');
    expect(req.request.urlWithParams).toContain('unread_only=true');
    req.flush({ items: [], unread_count: 3 });
    expect((await feed).unread_count).toBe(3);
  });

  it('marks everything read without ids', async () => {
    const done = notifications.markRead();
    const req = await nextRequest(controller, '/api/notifications/read', 'POST');
    expect(req.request.body).toEqual({});
    req.flush({ updated: 2, unread_count: 0 });
    expect(await done).toEqual({ updated: 2, unread_count: 0 });
  });

  it('reads the feed quietly for the bell', async () => {
    const feed = notifications.feed({ limit: 1 }, true);
    (await nextRequest(controller, '/api/notifications')).flush(
      { title: 'x', status: 500, detail: 'down' },
      { status: 500, statusText: 'Server Error' },
    );
    await expect(feed).rejects.toMatchObject({ status: 500 });
  });

  it('removes this browser by its endpoint', async () => {
    const done = notifications.unsubscribePush('https://push.example.com/abc');
    const req = await nextRequest(controller, '/api/push/subscriptions', 'DELETE');
    expect(req.request.body).toEqual({ endpoint: 'https://push.example.com/abc' });
    req.flush(null, { status: 204, statusText: 'No Content' });
    await done;
  });

  it('removes another device by id on the planned route', async () => {
    const done = notifications.removePushDevice('dev 1');
    const req = await nextRequest(controller, '/api/push/subscriptions/dev%201', 'DELETE');
    req.flush(null, { status: 405, statusText: 'Method Not Allowed' });
    await expect(done).rejects.toMatchObject({ status: 405 });
  });

  it('sets the webhook', async () => {
    const set = notifications.setWebhook('https://hooks.example.com/x');
    const req = await nextRequest(controller, '/api/notifications/webhook', 'PUT');
    expect(req.request.body).toEqual({ url: 'https://hooks.example.com/x' });
    req.flush({ webhook: 'https://hooks.example.com/***', preferences: [], channels: [] });
    expect((await set).webhook).toBe('https://hooks.example.com/***');
  });
});
