import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getBars, getDataCoverage, listCoverage, listInstruments } from './generated/sdk.gen';
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

  /** Which paid data kinds (fundamentals, calendars, news, options) are stored at all. */
  dataCoverage() {
    return unwrap(getDataCoverage());
  }
}
