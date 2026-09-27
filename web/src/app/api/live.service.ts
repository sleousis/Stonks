import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getAccountProfile,
  getBrokerGateways,
  getLiveAllocation,
  getLiveRules,
  setAccountProfile,
  setLiveAllocation,
} from './generated/sdk.gen';
import type { AccountProfileBody, AccountProfileView, LiveAllocationUpdate } from './models';

/**
 * A live portfolio's owner settings (the allocation and the account
 * profile), the live rules that act on it, and the broker gateways'
 * health. Both writes need a fresh second factor: the session interceptor
 * asks for a code when the API answers 403 `step_up_required`.
 */
@Injectable({ providedIn: 'root' })
export class LiveService {
  allocation(portfolioId: string) {
    return unwrap(getLiveAllocation({ path: { portfolio_id: portfolioId } }));
  }

  setAllocation(portfolioId: string, body: LiveAllocationUpdate) {
    return unwrap(setLiveAllocation({ path: { portfolio_id: portfolioId }, body }));
  }

  /** The account profile, or null while none is set (the API answers 404). */
  async profile(portfolioId: string): Promise<AccountProfileView | null> {
    const result = await getAccountProfile({ path: { portfolio_id: portfolioId } });
    if (result.error !== undefined && result.response?.status === 404) return null;
    return unwrap(Promise.resolve(result));
  }

  setProfile(portfolioId: string, body: AccountProfileBody) {
    return unwrap(setAccountProfile({ path: { portfolio_id: portfolioId }, body }));
  }

  rules(portfolioId: string) {
    return unwrap(getLiveRules({ path: { portfolio_id: portfolioId } }));
  }

  gateways() {
    return unwrap(getBrokerGateways());
  }
}
