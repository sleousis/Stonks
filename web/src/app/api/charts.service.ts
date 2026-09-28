import { Injectable, inject } from '@angular/core';

import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';
import { unwrap } from './api-call';
import { compareCharts, getChart } from './generated/sdk.gen';
import type { CompareChartsData, GetChartData } from './models';

/**
 * A price chart's data: bars, your fills in the picked portfolio, and
 * strategy signals; and several tickers compared on one scale with their
 * drawdown and rolling Sharpe.
 */
@Injectable({ providedIn: 'root' })
export class ChartsService {
  private readonly ctx = inject(PortfolioContextService);

  chart(ticker: string, query?: GetChartData['query']) {
    return unwrap(getChart({ path: { ticker }, query: { ...this.ctx.query(), ...query } }));
  }

  compare(tickers: readonly string[], query?: Omit<CompareChartsData['query'], 'tickers'>) {
    return unwrap(compareCharts({ query: { ...query, tickers: tickers.join(',') } }));
  }
}
