import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest } from '../../../testing/http';
import { HttpPushSubscriptionApi, PUSH_SUBSCRIPTION_API } from './push-subscription-api';

describe('HttpPushSubscriptionApi', () => {
  let controller: HttpTestingController;
  let api: HttpPushSubscriptionApi;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    api = TestBed.inject(HttpPushSubscriptionApi);
  });

  afterEach(() => controller.verify());

  it('is what PUSH_SUBSCRIPTION_API provides by default', () => {
    expect(TestBed.inject(PUSH_SUBSCRIPTION_API)).toBeInstanceOf(HttpPushSubscriptionApi);
  });

  it('reads the VAPID key', async () => {
    const key = api.vapidPublicKey();
    (await nextRequest(controller, '/api/push/vapid-key')).flush({ public_key: 'BKey' });
    expect(await key).toBe('BKey');
  });

  it('reports no key when the server cannot send push', async () => {
    const key = api.vapidPublicKey();
    (await nextRequest(controller, '/api/push/vapid-key')).flush({ public_key: null });
    expect(await key).toBeNull();
  });

  it('saves the subscription with the user agent', async () => {
    const done = api.save(
      { endpoint: 'https://fcm.googleapis.com/x', keys: { p256dh: 'p', auth: 'a' } },
      'Chrome',
    );
    const req = await nextRequest(controller, '/api/push/subscriptions', 'POST');
    expect(req.request.body).toEqual({
      endpoint: 'https://fcm.googleapis.com/x',
      keys: { p256dh: 'p', auth: 'a' },
      user_agent: 'Chrome',
    });
    req.flush({ id: 'psh_1', endpoint_host: 'fcm.googleapis.com', failure_count: 0 });
    await done;
  });

  it('removes a subscription by endpoint', async () => {
    const done = api.remove('https://fcm.googleapis.com/x');
    const req = await nextRequest(controller, '/api/push/subscriptions', 'DELETE');
    expect(req.request.body).toEqual({ endpoint: 'https://fcm.googleapis.com/x' });
    req.flush(null, { status: 204, statusText: 'No Content' });
    await done;
  });

  it('treats an unknown endpoint on removal as already gone', async () => {
    const done = api.remove('https://fcm.googleapis.com/gone');
    (await nextRequest(controller, '/api/push/subscriptions', 'DELETE')).flush(
      { title: 'Not Found', status: 404, detail: 'push subscription not found' },
      { status: 404, statusText: 'Not Found' },
    );
    await done;
  });
});
