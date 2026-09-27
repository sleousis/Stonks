import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import { listSubscriptions, subscribe, updateSubscription } from './generated/sdk.gen';
import type { SubscribeRequest, SubscriptionUpdate, SubscriptionView } from './models';

/*
 * The trader's strategy subscriptions (design: accounts-and-modes.md,
 * section 4), through the generated SDK.
 *
 *   GET   /api/subscriptions        -> the caller's own, paged
 *   POST  /api/subscriptions        <- { strategy_id, mode notify|paper, portfolio_id? }
 *         auto is never a starting mode (409)
 *   PATCH /api/subscriptions/{id}   <- { enabled?, mode?, reason? }
 *         mode 'auto' needs a fresh second factor (403 step_up_required)
 *         and passes the auto gate (409 with the blockers otherwise).
 */

export type { SubscribeRequest, SubscriptionUpdate, SubscriptionView };
export type SubscriptionMode = SubscriptionView['mode'];

@Injectable({ providedIn: 'root' })
export class SubscriptionsService {
  list(): Promise<SubscriptionView[]> {
    return allItems((query) => unwrap(listSubscriptions({ query })));
  }

  subscribe(body: SubscribeRequest): Promise<SubscriptionView> {
    return unwrap(subscribe({ body }));
  }

  update(id: string, body: SubscriptionUpdate): Promise<SubscriptionView> {
    return unwrap(updateSubscription({ path: { subscription_id: id }, body }));
  }
}
