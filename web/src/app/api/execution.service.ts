import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  clearExecutionAlgo,
  confirmRebalance,
  listAlgoParents,
  listExecutionAlgoSettings,
  listExecutionAlgos,
  planRebalance,
  setExecutionAlgo,
} from './generated/sdk.gen';
import type { AlgoSettingUpdate, PlanConfirm, PlanRequest } from './generated/types.gen';

/**
 * Execution algos and the rebalancing planner (roadmap 23.16). A plan
 * writes and sends nothing. A confirm writes order tickets that wait for
 * approval with a fresh second factor on the Approvals page.
 */
@Injectable({ providedIn: 'root' })
export class ExecutionService {
  algos() {
    return unwrap(listExecutionAlgos());
  }

  settings(portfolioId: string) {
    return unwrap(listExecutionAlgoSettings({ path: { portfolio_id: portfolioId } }));
  }

  setAlgo(portfolioId: string, body: AlgoSettingUpdate) {
    return unwrap(setExecutionAlgo({ path: { portfolio_id: portfolioId }, body }));
  }

  clearAlgo(portfolioId: string, strategyId?: string) {
    return unwrap(
      clearExecutionAlgo({
        path: { portfolio_id: portfolioId },
        query: strategyId ? { strategy_id: strategyId } : undefined,
      }),
    );
  }

  parents(portfolioId: string) {
    return unwrap(listAlgoParents({ path: { portfolio_id: portfolioId } }));
  }

  plan(body: PlanRequest) {
    return unwrap(planRebalance({ body }));
  }

  confirm(body: PlanConfirm) {
    return unwrap(confirmRebalance({ body }));
  }
}
