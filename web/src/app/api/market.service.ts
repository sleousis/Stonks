import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getBars, getMarketBreadth, listCoverage, listInstruments } from './generated/sdk.gen';
import type { GetBarsData, ListCoverageData, ListInstrumentsData } from './models';

/** Market data in the lake: bars, instruments, and coverage/freshness. */
@Injectable({ providedIn: 'root' })
export class MarketService {
  bars(query: GetBarsData['query']) {
    return unwrap(getBars({ query }));
  }

  instruments(query?: ListInstrumentsData['query']) {
    return unwrap(listInstruments({ query }));
  }

  coverage(query?: ListCoverageData['query']) {
    return unwrap(listCoverage({ query }));
  }

  /** Market breadth from the lake (roadmap 23.14). Display only. */
  breadth(asOf?: string) {
    return unwrap(getMarketBreadth({ query: asOf ? { as_of: asOf } : undefined }));
  }
}
