import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import {
  getCalendar,
  getEarningsWarnings,
  getNews,
  listEventAlertKinds,
} from './generated/sdk.gen';
import type { GetCalendarData, GetNewsData } from './models';

export type CalendarQuery = NonNullable<GetCalendarData['query']>;
export type NewsQuery = NonNullable<GetNewsData['query']>;

/** Earnings, ex-dividend and economic calendars, news, and the ticket's earnings check. */
@Injectable({ providedIn: 'root' })
export class CalendarsService {
  calendar(query: CalendarQuery) {
    return unwrap(getCalendar({ query }));
  }

  news(query: NewsQuery) {
    return unwrap(getNews({ query }));
  }

  /** Silent: the order ticket shows a warning line or nothing, never a toast. */
  earningsWarnings(tickers: readonly string[]) {
    return unwrap(
      getEarningsWarnings({ query: { tickers: tickers.join(',') }, headers: SILENT_HEADERS }),
    );
  }

  /** The upcoming-event alert kinds and how far ahead each looks. */
  alertKinds() {
    return unwrap(listEventAlertKinds({ headers: SILENT_HEADERS }));
  }
}
