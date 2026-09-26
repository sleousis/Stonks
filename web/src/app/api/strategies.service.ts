import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getGoLiveReport,
  getStrategy,
  listStrategies,
  promoteStrategy,
  retireStrategy,
  shadowStrategy,
} from './generated/sdk.gen';
import type { ListStrategiesData, StrategyStatus } from './models';

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

  get(strategyId: string) {
    return unwrap(getStrategy({ path: { strategy_id: strategyId } }));
  }

  /** The go-live gate's checks of the strategy's paper period (reports only). */
  golive(strategyId: string, since?: string) {
    return unwrap(
      getGoLiveReport({ path: { strategy_id: strategyId }, query: since ? { since } : undefined }),
    );
  }

  promote(strategyId: string) {
    return unwrap(promoteStrategy({ path: { strategy_id: strategyId } }));
  }

  shadow(strategyId: string) {
    return unwrap(shadowStrategy({ path: { strategy_id: strategyId } }));
  }

  retire(strategyId: string) {
    return unwrap(retireStrategy({ path: { strategy_id: strategyId } }));
  }
}
