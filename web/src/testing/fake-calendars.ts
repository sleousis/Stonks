import type { Provider } from '@angular/core';

import { CalendarsService } from '../app/api/calendars.service';

/**
 * A quiet CalendarsService for specs of pages that embed a calendar piece
 * (the order ticket's earnings line, the event alert list) but test
 * something else: no earnings warnings, no alert kinds, no HTTP.
 */
export function provideFakeCalendars(overrides: Partial<CalendarsService> = {}): Provider {
  return {
    provide: CalendarsService,
    useValue: {
      earningsWarnings: () => Promise.resolve({ checked: [], warnings: [] }),
      alertKinds: () => Promise.resolve([]),
      ...overrides,
    },
  };
}
