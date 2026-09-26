import { TestBed } from '@angular/core/testing';
import { SwPush } from '@angular/service-worker';
import { BehaviorSubject } from 'rxjs';

import {
  DEVICE_INFO,
  type DeviceInfo,
  NOTIFICATION_API,
  type NotificationApi,
  NotificationPermissionService,
} from './notification-permission.service';
import { PUSH_SUBSCRIPTION_API, type PushSubscriptionApi } from './push-subscription-api';

class FakeNotification implements NotificationApi {
  permission: NotificationPermission = 'default';
  answer: NotificationPermission = 'granted';
  requestPermission = vi.fn(async () => {
    this.permission = this.answer;
    return this.answer;
  });
}

const SUB = {
  endpoint: 'https://push.example/abc',
  toJSON: () => ({ endpoint: 'https://push.example/abc', keys: { p256dh: 'p', auth: 'a' } }),
};

function fakeSwPush(enabled = true) {
  const subscription = new BehaviorSubject<unknown>(null);
  return {
    isEnabled: enabled,
    subscription,
    requestSubscription: vi.fn(async () => {
      subscription.next(SUB);
      return SUB;
    }),
    unsubscribe: vi.fn(async () => subscription.next(null)),
  };
}

function fakeServer(
  key: string | null,
): PushSubscriptionApi & Record<string, ReturnType<typeof vi.fn>> {
  return {
    vapidPublicKey: vi.fn(async () => key),
    save: vi.fn(async () => undefined),
    remove: vi.fn(async () => undefined),
  };
}

interface Setup {
  notification?: FakeNotification | null;
  device?: Partial<DeviceInfo>;
  swPush?: ReturnType<typeof fakeSwPush> | null;
  server?: PushSubscriptionApi;
}

function setup(opts: Setup = {}) {
  const notification = opts.notification === undefined ? new FakeNotification() : opts.notification;
  const swPush = opts.swPush === undefined ? fakeSwPush() : opts.swPush;
  const server = opts.server ?? fakeServer('BPubKey');
  TestBed.configureTestingModule({
    providers: [
      { provide: NOTIFICATION_API, useValue: notification },
      {
        provide: DEVICE_INFO,
        useValue: { ios: false, standalone: false, userAgent: 'test', ...opts.device },
      },
      { provide: PUSH_SUBSCRIPTION_API, useValue: server },
      ...(swPush ? [{ provide: SwPush, useValue: swPush }] : []),
    ],
  });
  return { svc: TestBed.inject(NotificationPermissionService), notification, swPush, server };
}

describe('NotificationPermissionService', () => {
  it('reports unsupported browsers', () => {
    const { svc } = setup({ notification: null });
    expect(svc.state()).toBe('unsupported');
  });

  it('asks iPhone users to install the app first, and never prompts there', async () => {
    const { svc, notification } = setup({ device: { ios: true, standalone: false } });
    expect(svc.state()).toBe('install-first');
    await svc.enable();
    expect(notification!.requestPermission).not.toHaveBeenCalled();
  });

  it('works on an iPhone once installed to the home screen', () => {
    const { svc } = setup({ device: { ios: true, standalone: true } });
    expect(svc.state()).toBe('default');
  });

  it('does not ask on its own; asks on enable and subscribes with the VAPID key', async () => {
    const { svc, notification, swPush, server } = setup();
    expect(notification!.requestPermission).not.toHaveBeenCalled();
    expect(svc.state()).toBe('default');

    await svc.enable();
    expect(notification!.requestPermission).toHaveBeenCalledOnce();
    expect(svc.state()).toBe('granted');
    expect(swPush!.requestSubscription).toHaveBeenCalledWith({ serverPublicKey: 'BPubKey' });
    expect(server.save).toHaveBeenCalledWith(
      { endpoint: 'https://push.example/abc', keys: { p256dh: 'p', auth: 'a' } },
      'test',
    );
    expect(svc.push()).toBe('on');
  });

  it('stops at permission while the server has no push support', async () => {
    const { svc, swPush } = setup({ server: fakeServer(null) });
    await svc.enable();
    expect(svc.state()).toBe('granted');
    expect(svc.push()).toBe('waiting-for-server');
    expect(swPush!.requestSubscription).not.toHaveBeenCalled();
  });

  it('says when the service worker is not running', async () => {
    const { svc } = setup({ swPush: fakeSwPush(false) });
    await svc.enable();
    expect(svc.push()).toBe('no-worker');
  });

  it('records a denial and does not subscribe', async () => {
    const notification = new FakeNotification();
    notification.answer = 'denied';
    const { svc, swPush } = setup({ notification });
    await svc.enable();
    expect(svc.state()).toBe('denied');
    expect(swPush!.requestSubscription).not.toHaveBeenCalled();
    expect(svc.push()).toBe('off');
  });

  it('unsubscribes and tells the server on disable', async () => {
    const { svc, swPush, server } = setup();
    await svc.enable();
    await svc.disable();
    expect(server.remove).toHaveBeenCalledWith('https://push.example/abc');
    expect(swPush!.unsubscribe).toHaveBeenCalledOnce();
    expect(svc.push()).toBe('off');
  });

  it('shows a readable error when subscribing fails', async () => {
    const swPush = fakeSwPush();
    swPush.requestSubscription.mockRejectedValueOnce(new Error('push service unreachable'));
    const { svc } = setup({ swPush });
    await svc.enable();
    expect(svc.error()).toContain('push service unreachable');
    expect(svc.busy()).toBe(false);
  });

  it('picks up an existing subscription on refresh', async () => {
    const notification = new FakeNotification();
    notification.permission = 'granted';
    const swPush = fakeSwPush();
    swPush.subscription.next(SUB);
    const { svc } = setup({ notification, swPush });
    await svc.refresh();
    expect(svc.push()).toBe('on');
  });
});
