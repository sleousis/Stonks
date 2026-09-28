import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getAssistantResearch,
  listAssistantResearch,
  startAssistantResearch,
} from './generated/sdk.gen';
import type { ListAssistantResearchData, ResearchStart } from './models';

/**
 * The assistant's research sessions (roadmap 22.9): the model proposes lab
 * trials under a budget, and the lab runs them. Never registers.
 */
@Injectable({ providedIn: 'root' })
export class ResearchService {
  /** Your sessions, newest first (one page). */
  list(query?: ListAssistantResearchData['query']) {
    return unwrap(listAssistantResearch({ query }));
  }

  get(id: string) {
    return unwrap(getAssistantResearch({ path: { session_id: id } }));
  }

  /** Starts a session as a background job; follow it, then read the session. */
  start(body: ResearchStart) {
    return unwrap(startAssistantResearch({ body }));
  }
}
