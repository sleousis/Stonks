import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import { listSubscriptions, updateSubscription } from './generated/sdk.gen';
import type { SubscriptionUpdate, SubscriptionView } from './models';

/*
 * The trader's strategy subscriptions (design: accounts-and-modes.md,
 * section 4), through the generated SDK.
 *
 *   GET   /api/subscriptions        -> the caller's own, paged
 *   PATCH /api/subscriptions/{id}   <- { enabled?, mode?, reason? }
 *         mode 'auto' needs a fresh second factor (403 step_up_required)
 *         and passes the auto gate (409 with the blockers otherwise).
 */

export type { SubscriptionUpdate, SubscriptionView };
export type SubscriptionMode = SubscriptionView['mode'];

@Injectable({ providedIn: 'root' })
export class SubscriptionsService {
  list(): Promise<SubscriptionView[]> {
    return allItems((query) => unwrap(listSubscriptions({ query })));
  }

  update(id: string, body: SubscriptionUpdate): Promise<SubscriptionView> {
    return unwrap(updateSubscription({ path: { subscription_id: id }, body }));
  }
}
