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

  it('sets the webhook', async () => {
    const set = notifications.setWebhook('https://hooks.example.com/x');
    const req = await nextRequest(controller, '/api/notifications/webhook', 'PUT');
    expect(req.request.body).toEqual({ url: 'https://hooks.example.com/x' });
    req.flush({ webhook: 'https://hooks.example.com/***', preferences: [], channels: [] });
    expect((await set).webhook).toBe('https://hooks.example.com/***');
  });
});
