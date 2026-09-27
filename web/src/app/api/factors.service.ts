import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import {
  checkFactorExpression,
  getFactor,
  getFactorTearsheetResult,
  getFactorValues,
  listFactors,
  startFactorTearsheet,
} from './generated/sdk.gen';
import type { FactorTearSheetRequest, FactorValuesRequest, ListFactorsData } from './models';

/**
 * The factor library, the formula checker, factor values on a date and
 * factor tear sheets (a background job).
 */
@Injectable({ providedIn: 'root' })
export class FactorsService {
  /** The library with its sets and families, optionally narrowed. */
  list(query?: ListFactorsData['query']) {
    return unwrap(listFactors({ query }));
  }

  get(id: string) {
    return unwrap(getFactor({ path: { factor_id: id } }));
  }

  /** Parses a formula; silent, because the editor shows the answer inline. */
  check(expression: string) {
    return unwrap(checkFactorExpression({ body: { expression }, headers: SILENT_HEADERS }));
  }

  /** A read sent as a POST; silent, because the panel shows its own error. */
  values(body: FactorValuesRequest) {
    return unwrap(getFactorValues({ body, headers: SILENT_HEADERS }));
  }

  startTearsheet(body: FactorTearSheetRequest) {
    return unwrap(startFactorTearsheet({ body }));
  }

  tearsheetResult(jobId: string) {
    return unwrap(getFactorTearsheetResult({ path: { job_id: jobId } }));
  }
}
