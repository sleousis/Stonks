import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  createPushSubscription,
  deletePushDevice,
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
    return allItems((query) => unwrap(listPushSubscriptions({ query })));
  }

  /** Remove another registered device by the id the device list shows. Silent. */
  async removePushDevice(id: string): Promise<void> {
    await unwrap(deletePushDevice({ path: { device_id: id }, headers: SILENT_HEADERS }));
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

  /** `silent` skips error toasts (the bell's background refresh). */
  feed(query?: ListNotificationsData['query'], silent = false) {
    return unwrap(listNotifications({ query, headers: silent ? SILENT_HEADERS : undefined }));
  }

  /** Mark these ids read, or every notification when `ids` is omitted. */
  markRead(ids?: number[]) {
    return unwrap(markNotificationsRead({ body: ids ? { ids } : {} }));
  }
}
