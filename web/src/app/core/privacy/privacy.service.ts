import { DOCUMENT } from '@angular/common';
import { Injectable, inject, signal } from '@angular/core';

import { moneyHidden } from '../format/format';

export const PRIVACY_STORAGE_KEY = 'stonks.privacy';

/**
 * Privacy mode (roadmap 23.17): one switch hides every money amount in the
 * console. Percentages stay, so a person can still read how a book is doing
 * while sharing a screen.
 *
 * - `formatMoney` (and the money pipe, tables, tiles and chart axes that use
 *   it) shows a mask while it is on.
 * - `html[data-privacy="on"]` lets a page blur a figure it formats itself:
 *   give the element the `money` class.
 * - The choice is per device (localStorage), not per account, so hiding
 *   amounts on a laptop in a café leaves the desk screen alone.
 *
 * Toggled by the eye button in the top bar and sidebar, the palette, the
 * `h` key and Alt+Shift+H.
 */
@Injectable({ providedIn: 'root' })
export class PrivacyService {
  private readonly doc = inject(DOCUMENT);
  private readonly state = signal(readHidden());

  /** True while money amounts are hidden. */
  readonly hidden = this.state.asReadonly();

  constructor() {
    this.apply(this.state());
  }

  toggle(): void {
    this.set(!this.state());
  }

  set(hidden: boolean): void {
    this.state.set(hidden);
    this.apply(hidden);
    try {
      if (hidden) localStorage.setItem(PRIVACY_STORAGE_KEY, 'on');
      else localStorage.removeItem(PRIVACY_STORAGE_KEY);
    } catch {
      // Storage blocked: the choice still applies for this page view.
    }
  }

  private apply(hidden: boolean): void {
    moneyHidden.set(hidden);
    const root = this.doc.documentElement;
    if (hidden) root.dataset['privacy'] = 'on';
    else delete root.dataset['privacy'];
  }
}

function readHidden(): boolean {
  try {
    return localStorage.getItem(PRIVACY_STORAGE_KEY) === 'on';
  } catch {
    return false;
  }
}
