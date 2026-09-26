import { Injectable, InjectionToken, computed, inject, signal } from '@angular/core';
import { SwPush } from '@angular/service-worker';
import { firstValueFrom } from 'rxjs';

import { PUSH_SUBSCRIPTION_API, type PushSubscriptionData } from './push-subscription-api';

/** The slice of the browser's `Notification` global this service uses. */
export interface NotificationApi {
  readonly permission: NotificationPermission;
  requestPermission(): Promise<NotificationPermission>;
}

export const NOTIFICATION_API = new InjectionToken<NotificationApi | null>('NOTIFICATION_API', {
  providedIn: 'root',
  factory: () =>
    typeof globalThis.Notification === 'undefined'
      ? null
      : (globalThis.Notification as NotificationApi),
});

export interface DeviceInfo {
  /** iPhone or iPad (Safari delivers Web Push only to installed web apps there). */
  ios: boolean;
  /** Running as an installed app (home screen / standalone window). */
  standalone: boolean;
  userAgent: string;
}

export const DEVICE_INFO = new InjectionToken<DeviceInfo>('DEVICE_INFO', {
  providedIn: 'root',
  factory: () => {
    const nav = globalThis.navigator as (Navigator & { standalone?: boolean }) | undefined;
    const ua = nav?.userAgent ?? '';
    // iPadOS reports itself as a Mac with touch.
    const ios =
      /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (nav?.maxTouchPoints ?? 0) > 1);
    const standalone =
      nav?.standalone === true ||
      (globalThis.matchMedia?.('(display-mode: standalone)').matches ?? false);
    return { ios, standalone, userAgent: ua };
  },
});

/**
 * - `unsupported`: this browser cannot show notifications.
 * - `install-first`: iPhone/iPad in Safari: add to the Home Screen first.
 * - `default`: not asked yet. `denied`: blocked in browser settings.
 * - `granted`: allowed.
 */
export type NotificationState = 'unsupported' | 'install-first' | NotificationPermission;

/**
 * - `off`: not subscribed. `on`: subscribed and registered with the server.
 * - `waiting-for-server`: allowed, but the server has no push support yet.
 * - `no-worker`: allowed, but the service worker is not running (dev server).
 */
export type PushStatus = 'off' | 'on' | 'waiting-for-server' | 'no-worker';

/**
 * Notification permission and Web Push subscription for this browser.
 * Asks only when the trader presses the button in Settings (never on load),
 * then subscribes through Angular's service worker (SwPush) with the
 * server's VAPID key and hands the subscription to PUSH_SUBSCRIPTION_API.
 */
@Injectable({ providedIn: 'root' })
export class NotificationPermissionService {
  private readonly api = inject(NOTIFICATION_API);
  private readonly device = inject(DEVICE_INFO);
  private readonly swPush = inject(SwPush, { optional: true });
  private readonly server = inject(PUSH_SUBSCRIPTION_API);

  private readonly permission = signal<NotificationPermission | null>(this.api?.permission ?? null);
  readonly push = signal<PushStatus>('off');
  readonly busy = signal(false);
  /** Last failure, in words for the Settings panel. */
  readonly error = signal<string | null>(null);

  readonly state = computed<NotificationState>(() => {
    if (this.device.ios && !this.device.standalone) return 'install-first';
    return this.permission() ?? 'unsupported';
  });

  /** Re-read the permission (the trader may have changed it in browser settings). */
  async refresh(): Promise<void> {
    this.permission.set(this.api?.permission ?? null);
    if (this.permission() !== 'granted' || !this.swPush?.isEnabled) return;
    const existing = await firstValueFrom(this.swPush.subscription);
    if (existing) this.push.set('on');
  }

  /** Ask for permission (from a click), then subscribe to push. */
  async enable(): Promise<void> {
    if (!this.api || this.state() === 'install-first' || this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      const result =
        this.api.permission === 'default'
          ? await this.api.requestPermission()
          : this.api.permission;
      this.permission.set(result);
      if (result === 'granted') await this.subscribe();
    } catch (err) {
      this.error.set(
        `Could not turn on notifications: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.busy.set(false);
    }
  }

  /** Stop push to this browser (the permission itself stays with the browser). */
  async disable(): Promise<void> {
    if (this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      if (this.swPush?.isEnabled) {
        const sub = await firstValueFrom(this.swPush.subscription);
        if (sub) {
          await this.server.remove(sub.endpoint);
          await this.swPush.unsubscribe();
        }
      }
      this.push.set('off');
    } catch (err) {
      this.error.set(
        `Could not turn off notifications: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.busy.set(false);
    }
  }

  private async subscribe(): Promise<void> {
    if (!this.swPush?.isEnabled) {
      this.push.set('no-worker');
      return;
    }
    const key = await this.server.vapidPublicKey();
    if (!key) {
      this.push.set('waiting-for-server');
      return;
    }
    const sub = await this.swPush.requestSubscription({ serverPublicKey: key });
    await this.server.save(sub.toJSON() as PushSubscriptionData, this.device.userAgent);
    this.push.set('on');
  }
}
