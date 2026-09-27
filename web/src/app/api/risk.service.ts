import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';

import { unwrap } from './api-call';
import { getLiveRisk, listRiskSnapshots } from './generated/sdk.gen';
import type { ListRiskSnapshotsData } from './models';

/** Live risk (VaR, ES, violations, decay) and its daily history, for the picked portfolio. */
@Injectable({ providedIn: 'root' })
export class RiskService {
  private readonly ctx = inject(PortfolioContextService);

  live() {
    return unwrap(getLiveRisk({ query: this.ctx.query() }));
  }

  snapshots(query?: ListRiskSnapshotsData['query']) {
    return unwrap(listRiskSnapshots({ query: { ...this.ctx.query(), ...query } }));
  }
}
