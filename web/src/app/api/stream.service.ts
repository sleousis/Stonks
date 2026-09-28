import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getStreamStatus } from './generated/sdk.gen';

/** The live intraday engine: stream health, latency and the silent-engine alarm. */
@Injectable({ providedIn: 'root' })
export class StreamService {
  status() {
    return unwrap(getStreamStatus());
  }
}
