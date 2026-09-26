import { Injectable, InjectionToken } from '@angular/core';

/** The browser's push subscription as JSON (endpoint + p256dh/auth keys). */
export interface PushSubscriptionData {
  endpoint: string;
  expirationTime?: number | null;
  keys: { p256dh: string; auth: string };
}

/**
 * Where the console registers this browser for Web Push. The backend side
 * (docs/design/accounts-and-modes.md, "Web Push") is:
 *
 *   GET    /api/push/vapid-key            -> { public_key }   (VAPID, P-256, base64url)
 *   POST   /api/push/subscriptions        <- { endpoint, keys: { p256dh, auth }, user_agent }
 *   DELETE /api/push/subscriptions        <- { endpoint }
 *
 * Until those routes exist the pending implementation below reports "no
 * key", and the console stops after asking for permission. Swap it for an
 * HTTP implementation (a domain service in src/app/api/) by providing
 * PUSH_SUBSCRIPTION_API; nothing else changes.
 */
export interface PushSubscriptionApi {
  /** The server's VAPID public key, or null when the server has no push yet. */
  vapidPublicKey(): Promise<string | null>;
  save(subscription: PushSubscriptionData, userAgent: string): Promise<void>;
  remove(endpoint: string): Promise<void>;
}

/** No server support yet: never subscribes. */
@Injectable({ providedIn: 'root' })
export class PendingPushSubscriptionApi implements PushSubscriptionApi {
  async vapidPublicKey(): Promise<string | null> {
    return null;
  }

  async save(): Promise<void> {
    // Nothing to save to until the backend adds /api/push/subscriptions.
  }

  async remove(): Promise<void> {
    // Nothing to remove.
  }
}

export const PUSH_SUBSCRIPTION_API = new InjectionToken<PushSubscriptionApi>(
  'PUSH_SUBSCRIPTION_API',
  { providedIn: 'root', factory: () => new PendingPushSubscriptionApi() },
);
