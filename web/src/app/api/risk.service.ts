import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import {
  getLiveRisk,
  getMyRiskLimits,
  listRiskSnapshots,
  setMyRiskLimits,
} from './generated/sdk.gen';
import type { ListRiskSnapshotsData } from './models';

/** Live risk (VaR, ES, violations, decay) and its daily history, for the picked portfolio. */
@Injectable({ providedIn: 'root' })
export class RiskService {
  private readonly ctx = inject(PortfolioContextService);

  live() {
    return unwrap(getLiveRisk({ query: this.ctx.query() }));
  }

  /** Your own limits next to the system policy (not per portfolio). */
  myLimits() {
    return unwrap(getMyRiskLimits());
  }

  /** Replace your own limits; they only ever tighten the system policy. */
  setMyLimits(limits: Record<string, unknown>) {
    return unwrap(setMyRiskLimits({ body: { limits } }));
  }

  snapshots(query?: ListRiskSnapshotsData['query']) {
    return unwrap(listRiskSnapshots({ query: { ...this.ctx.query(), ...query } }));
  }
}
