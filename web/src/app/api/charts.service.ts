import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';
import { unwrap } from './api-call';
import { getChart } from './generated/sdk.gen';
import type { GetChartData } from './models';

/** A price chart's data: bars, your fills in the picked portfolio, and strategy signals. */
@Injectable({ providedIn: 'root' })
export class ChartsService {
  private readonly ctx = inject(PortfolioContextService);

  chart(ticker: string, query?: GetChartData['query']) {
    return unwrap(getChart({ path: { ticker }, query: { ...this.ctx.query(), ...query } }));
  }
}
