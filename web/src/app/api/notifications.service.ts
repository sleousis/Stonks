import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import {
  createPushSubscription,
  deletePushSubscription,
  getNotificationPreferences,
  getVapidKey,
  listNotifications,
  listPushSubscriptions,
  markNotificationsRead,
  setNotificationWebhook,
  setQuietHours,
  updateNotificationPreferences,
} from './generated/sdk.gen';
import type {
  ListNotificationsData,
  PreferencesUpdate,
  PushSubscriptionRequest,
  QuietHoursUpdate,
} from './models';

/**
 * The signed-in user's notifications: Web Push devices, preferences, quiet
 * hours, their webhook (write-only) and the in-app feed.
 *
 * Push calls are silent (no error toast): the Settings panel that makes
 * them reports failures itself.
 */
@Injectable({ providedIn: 'root' })
export class NotificationsService {
  vapidKey() {
    return unwrap(getVapidKey({ headers: SILENT_HEADERS }));
  }

  subscribePush(body: PushSubscriptionRequest) {
    return unwrap(createPushSubscription({ body, headers: SILENT_HEADERS }));
  }

  unsubscribePush(endpoint: string) {
    return unwrap(deletePushSubscription({ body: { endpoint }, headers: SILENT_HEADERS }));
  }

  pushDevices() {
    return unwrap(listPushSubscriptions());
  }

  preferences() {
    return unwrap(getNotificationPreferences());
  }

  updatePreferences(body: PreferencesUpdate) {
    return unwrap(updateNotificationPreferences({ body }));
  }

  setQuietHours(body: QuietHoursUpdate) {
    return unwrap(setQuietHours({ body }));
  }

  /** Null removes the webhook. The response shows its scheme and host only. */
  setWebhook(url: string | null) {
    return unwrap(setNotificationWebhook({ body: { url } }));
  }

  feed(query?: ListNotificationsData['query']) {
    return unwrap(listNotifications({ query }));
  }

  /** Mark these ids read, or every notification when `ids` is omitted. */
  markRead(ids?: number[]) {
    return unwrap(markNotificationsRead({ body: ids ? { ids } : {} }));
  }
}
