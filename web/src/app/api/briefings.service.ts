import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getBriefingPrefs, setBriefingPrefs } from './generated/sdk.gen';
import type { BriefingPrefsUpdate } from './models';

/**
 * Research-only briefings before the open and after the close, written by
 * the assistant from read tools and sent through your alert channels.
 */
@Injectable({ providedIn: 'root' })
export class BriefingsService {
  prefs() {
    return unwrap(getBriefingPrefs());
  }

  save(body: BriefingPrefsUpdate) {
    return unwrap(setBriefingPrefs({ body }));
  }
}
