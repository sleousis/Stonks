import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  demoteLiveStage,
  getAccountProfile,
  getBrokerGateways,
  getLiveAllocation,
  getLiveGateReport,
  getLiveMargin,
  getLiveRules,
  listReconcileReports,
  getLiveStage,
  getOptionsLive,
  previewLiveOrders,
  promoteLiveStage,
  setAccountProfile,
  setLiveAllocation,
  setOptionsApproval,
} from './generated/sdk.gen';
import type {
  AccountProfileBody,
  AccountProfileView,
  LiveAllocationUpdate,
  OptionsApprovalUpdate,
  StageDemoteBody,
  StagePromoteBody,
} from './models';

/**
 * A live portfolio's owner settings (the allocation and the account
 * profile), the live rules that act on it, its buying power and margin use, the broker gateways' health,
 * the reconciliation reports of your live portfolios, and the live stage
 * (roadmap 19.9): its gate report, promotion, demotion and the dry-run
 * preview. The settings writes and a promotion need a fresh second factor:
 * the session interceptor asks for a code when the API answers 403
 * `step_up_required`. A demotion and a preview do not.
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

  /** Buying power and margin use, read from the broker now (roadmap 19.13). */
  margin(portfolioId: string) {
    return unwrap(getLiveMargin({ path: { portfolio_id: portfolioId } }));
  }

  gateways() {
    return unwrap(getBrokerGateways());
  }

  /** The latest checks of your live portfolios against their brokers. */
  reconcileReports(limit = 10) {
    return unwrap(listReconcileReports({ query: { limit } })).then((page) => page.items);
  }

  stage(portfolioId: string, days = 20) {
    return unwrap(getLiveStage({ path: { portfolio_id: portfolioId }, query: { days } }));
  }

  gateReport(portfolioId: string) {
    return unwrap(getLiveGateReport({ path: { portfolio_id: portfolioId } }));
  }

  promote(portfolioId: string, body: StagePromoteBody) {
    return unwrap(promoteLiveStage({ path: { portfolio_id: portfolioId }, body }));
  }

  demote(portfolioId: string, body: StageDemoteBody) {
    return unwrap(demoteLiveStage({ path: { portfolio_id: portfolioId }, body }));
  }

  /** Whether options may open here (off by default), and why not. */
  optionsLive(portfolioId: string) {
    return unwrap(getOptionsLive({ path: { portfolio_id: portfolioId } }));
  }

  /** The options approval level. Needs a fresh second factor. */
  setOptionsApproval(portfolioId: string, body: OptionsApprovalUpdate) {
    return unwrap(setOptionsApproval({ path: { portfolio_id: portfolioId }, body }));
  }

  /** A dry run through the broker's what-if. It never sends an order. */
  preview(portfolioId: string) {
    return unwrap(previewLiveOrders({ path: { portfolio_id: portfolioId } }));
  }
}
