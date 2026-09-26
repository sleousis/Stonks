import { DOCUMENT } from '@angular/common';
import { InjectionToken, inject } from '@angular/core';

/**
 * Leaving the console for a provider's hosted sign-in page. A seam so
 * tests can check the URL without navigating the test browser away.
 */
export interface BrowserRedirect {
  /** This console's origin, e.g. `https://stonks.example.com`. */
  origin(): string;
  /** Send the whole tab to `url`. */
  go(url: string): void;
}

export const BROWSER_REDIRECT = new InjectionToken<BrowserRedirect>('BrowserRedirect', {
  providedIn: 'root',
  factory: () => {
    const location = inject(DOCUMENT).location;
    return {
      origin: () => location.origin,
      go: (url: string) => location.assign(url),
    };
  },
});
