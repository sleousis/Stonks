import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import {
  getOptionChain,
  getOptionPayoff,
  getOptionsBacktestResult,
  listOptionStrategies,
  listOptionStructures,
  listOptionUnderlyings,
  startOptionsBacktest,
} from './generated/sdk.gen';
import type { OptionPayoffRequest, OptionsBacktestRequest } from './models';

/**
 * Options research: stored chains with Greeks, the options strategies and
 * structures, a structure's payoff and options backtests (a background
 * job). Research only, nothing here trades options.
 */
@Injectable({ providedIn: 'root' })
export class OptionsService {
  underlyings() {
    return unwrap(listOptionUnderlyings());
  }

  /** One expiry of a stored chain; the last stored day on or before `asOf`. */
  chain(underlying: string, asOf?: string | null, expiry?: string | null) {
    return unwrap(
      getOptionChain({
        path: { underlying },
        query: { as_of: asOf || undefined, expiry: expiry || undefined },
      }),
    );
  }

  strategies() {
    return unwrap(listOptionStrategies());
  }

  structures() {
    return unwrap(listOptionStructures());
  }

  /** A read sent as a POST; silent, because the panel shows its own error. */
  payoff(body: OptionPayoffRequest) {
    return unwrap(getOptionPayoff({ body, headers: SILENT_HEADERS }));
  }

  startBacktest(body: OptionsBacktestRequest) {
    return unwrap(startOptionsBacktest({ body }));
  }

  backtestResult(jobId: string) {
    return unwrap(getOptionsBacktestResult({ path: { job_id: jobId } }));
  }
}
