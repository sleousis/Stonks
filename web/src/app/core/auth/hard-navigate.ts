import { InjectionToken } from '@angular/core';

/**
 * A full page load, not a router navigation. Signing out (or a different
 * user signing in on the same tab) goes through this so every root store
 * (portfolio context, halts, trading day, alerts, feed, watchlists) starts
 * empty and nothing of the last user lingers (UX-07). Tests replace it.
 */
export const HARD_NAVIGATE = new InjectionToken<(url: string) => void>('HARD_NAVIGATE', {
  providedIn: 'root',
  factory: () => (url: string) => window.location.assign(url),
});

/** The current in-app URL (path, query and hash), for a reload in place. */
export function currentUrl(): string {
  try {
    const { pathname, search, hash } = window.location;
    return `${pathname}${search}${hash}`;
  } catch {
    return '/';
  }
}
