import type { Provider } from '@angular/core';

import type { DataCoverage } from '../app/api/models';
import { DataCoverageService, type PaidDataKind } from '../app/shared/ui/data-plan-note';

/** Every paid data kind stored: data-plan notes stay hidden. */
export const ALL_DATA: DataCoverage = {
  fundamentals: true,
  calendars: true,
  news: true,
  options: true,
};

/**
 * A DataCoverageService with fixed answers, for specs of pages that show a
 * data-plan note but test something else: no HTTP. Every kind is stored by
 * default, so no note shows.
 */
export function provideFakeDataCoverage(coverage: Partial<DataCoverage> = {}): Provider {
  const known = { ...ALL_DATA, ...coverage };
  return {
    provide: DataCoverageService,
    useValue: { has: (kind: PaidDataKind) => known[kind] },
  };
}
