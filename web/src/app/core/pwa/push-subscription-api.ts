import { Injectable, InjectionToken, inject } from '@angular/core';

import { NotificationsService } from '../../api/notifications.service';
import { ApiError } from '../http/api-error';

/** The browser's push subscription as JSON (endpoint + p256dh/auth keys). */
export interface PushSubscriptionData {
  endpoint: string;
  expirationTime?: number | null;
  keys: { p256dh: string; auth: string };
}

/**
 * Where the console registers this browser for Web Push
 * (docs/design/accounts-and-modes.md, "Web Push"):
 *
 *   GET    /api/push/vapid-key            -> { public_key }   (VAPID, P-256, base64url)
 *   POST   /api/push/subscriptions        <- { endpoint, keys: { p256dh, auth }, user_agent }
 *   DELETE /api/push/subscriptions        <- { endpoint }
 *
 * The routes need the API token (they act for the signed-in user).
 */
export interface PushSubscriptionApi {
  /** The server's VAPID public key, or null when the server can't send push. */
  vapidPublicKey(): Promise<string | null>;
  save(subscription: PushSubscriptionData, userAgent: string): Promise<void>;
  remove(endpoint: string): Promise<void>;
}

/** The HTTP implementation over the generated client. */
@Injectable({ providedIn: 'root' })
export class HttpPushSubscriptionApi implements PushSubscriptionApi {
  private readonly notifications = inject(NotificationsService);

  async vapidPublicKey(): Promise<string | null> {
    const { public_key } = await this.notifications.vapidKey();
    return public_key ?? null;
  }

  async save(subscription: PushSubscriptionData, userAgent: string): Promise<void> {
    await this.notifications.subscribePush({
      endpoint: subscription.endpoint,
      keys: { p256dh: subscription.keys.p256dh, auth: subscription.keys.auth },
      user_agent: userAgent,
    });
  }

  async remove(endpoint: string): Promise<void> {
    try {
      await this.notifications.unsubscribePush(endpoint);
    } catch (err) {
      // Already unknown to the server (removed elsewhere): nothing to undo.
      if (!(err instanceof ApiError && err.status === 404)) throw err;
    }
  }
}

export const PUSH_SUBSCRIPTION_API = new InjectionToken<PushSubscriptionApi>(
  'PUSH_SUBSCRIPTION_API',
  { providedIn: 'root', factory: () => inject(HttpPushSubscriptionApi) },
);
