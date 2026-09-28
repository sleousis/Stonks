import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getDemoPortfolio, openDemoPortfolio, removeDemoPortfolio } from './generated/sdk.gen';

/** Your demo portfolio: sample data only, never a real book (roadmap 23.17). */
@Injectable({ providedIn: 'root' })
export class DemoService {
  get() {
    return unwrap(getDemoPortfolio());
  }

  open() {
    return unwrap(openDemoPortfolio());
  }

  remove() {
    return unwrap(removeDemoPortfolio());
  }
}
