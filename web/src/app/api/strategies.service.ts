import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  getGoLiveReport,
  getLeaderboard,
  getStrategy,
  getStrategyHistory,
  getStrategySummary,
  getTearSheet,
  listStrategies,
  promoteStrategy,
  retireStrategy,
  shadowStrategy,
} from './generated/sdk.gen';
import type {
  GetLeaderboardData,
  ListStrategiesData,
  StatusChangeRequest,
  StrategyStatus,
} from './models';

/** Registered strategies, their survival reports, and status changes. */
@Injectable({ providedIn: 'root' })
export class StrategiesService {
  list(query?: ListStrategiesData['query']) {
    return unwrap(listStrategies({ query }));
  }

  /** Number of strategies with a status (one row fetched, `total` read). */
  async count(status: StrategyStatus): Promise<number> {
    const page = await this.list({ status, limit: 1 });
    return page.total;
  }

  /** How many strategies are active, in shadow and retired, in one call. */
  summary() {
    return unwrap(getStrategySummary());
  }

  get(strategyId: string) {
    return unwrap(getStrategy({ path: { strategy_id: strategyId } }));
  }

  /** The go-live gate's checks of the strategy's paper period (reports only). */
  golive(strategyId: string, since?: string) {
    return unwrap(
      getGoLiveReport({ path: { strategy_id: strategyId }, query: since ? { since } : undefined }),
    );
  }

  /** Every strategy ranked by its risk-adjusted paper result. */
  leaderboard(query?: GetLeaderboardData['query']) {
    return unwrap(getLeaderboard({ query }));
  }

  /** One strategy on one page, from stored results. */
  tearSheet(strategyId: string) {
    return unwrap(getTearSheet({ path: { strategy_id: strategyId } }));
  }

  /** Audited status changes and interventions, oldest first. */
  history(strategyId: string) {
    return allItems((query) =>
      unwrap(getStrategyHistory({ path: { strategy_id: strategyId }, query })),
    );
  }

  /**
   * Move to active. The API answers 409 when the go-live gate refuses; send
   * `override` with a reason of at least 20 characters to promote anyway.
   * `silent` skips the error toast (the caller handles the 409 itself).
   */
  promote(strategyId: string, body: StatusChangeRequest, silent = false) {
    return unwrap(
      promoteStrategy({
        path: { strategy_id: strategyId },
        body,
        headers: silent ? SILENT_HEADERS : undefined,
      }),
    );
  }

  /** Move to shadow; `reason` is required. */
  shadow(strategyId: string, body: StatusChangeRequest) {
    return unwrap(shadowStrategy({ path: { strategy_id: strategyId }, body }));
  }

  /** Move to retired; `reason` is required. */
  retire(strategyId: string, body: StatusChangeRequest) {
    return unwrap(retireStrategy({ path: { strategy_id: strategyId }, body }));
  }
}
