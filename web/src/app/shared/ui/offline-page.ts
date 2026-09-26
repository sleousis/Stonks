import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';

import { ConnectivityService } from '../../core/pwa/connectivity.service';

/**
 * Shown by the shell in place of the page while the browser is offline.
 * The service worker caches only the app shell, never API data, so there is
 * nothing trustworthy to show; the page comes back, freshly loaded, as soon
 * as the connection does.
 */
@Component({
  selector: 'app-offline-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="offline" aria-labelledby="offline-title">
      <svg viewBox="0 0 48 48" width="48" height="48" aria-hidden="true">
        <path
          d="M6 18a26 26 0 0 1 36 0M12 25a17 17 0 0 1 24 0M18 32a8 8 0 0 1 12 0"
          fill="none"
          stroke="currentColor"
          stroke-width="3"
          stroke-linecap="round"
        />
        <path d="M8 8l32 32" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
      </svg>
      <h1 id="offline-title" tabindex="-1">You're offline</h1>
      <p>
        Figures and actions need the Stonks API. Nothing is shown from an old copy, so every number
        you see is current. This page comes back on its own when you reconnect.
      </p>
      <button type="button" class="btn btn-primary" (click)="retry()">Try again</button>
      <p class="still" role="status">{{ still() }}</p>
    </section>
  `,
  styles: `
    .offline {
      display: grid;
      justify-items: start;
      gap: var(--space-3);
      max-width: 34rem;
      margin: var(--space-7) auto;
      padding: var(--space-5);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-md);
      background: var(--color-surface);
    }
    svg {
      color: var(--color-warn);
    }
    h1 {
      font-size: var(--text-xl);
    }
    p {
      color: var(--color-ink-2);
    }
    .still {
      font-size: var(--text-sm);
    }
  `,
})
export class OfflinePage {
  private readonly connectivity = inject(ConnectivityService);

  protected readonly still = signal('');

  protected retry(): void {
    const online = globalThis.navigator?.onLine ?? true;
    this.connectivity.online.set(online);
    if (!online) this.still.set('Still offline. Check Wi-Fi or mobile data.');
  }
}
