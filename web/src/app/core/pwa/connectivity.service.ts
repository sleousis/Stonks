import { DOCUMENT } from '@angular/common';
import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { SwUpdate } from '@angular/service-worker';

import { ToastService } from '../notify/toast.service';

/**
 * Online/offline state and app updates.
 *
 * Offline, the service worker still serves the app shell (never API data),
 * so the console opens; the shell then shows an offline notice and every
 * panel shows its own error state until the connection returns.
 *
 * When the service worker has downloaded a new version it says so once,
 * with a toast; the next page load uses it.
 */
@Injectable({ providedIn: 'root' })
export class ConnectivityService {
  private readonly win = inject(DOCUMENT).defaultView;
  private readonly updates = inject(SwUpdate, { optional: true });
  private readonly toasts = inject(ToastService);

  readonly online = signal(this.win?.navigator.onLine ?? true);

  constructor() {
    const set = () => this.online.set(this.win?.navigator.onLine ?? true);
    this.win?.addEventListener('online', set);
    this.win?.addEventListener('offline', set);
    inject(DestroyRef).onDestroy(() => {
      this.win?.removeEventListener('online', set);
      this.win?.removeEventListener('offline', set);
    });

    if (this.updates?.isEnabled) {
      this.updates.versionUpdates.subscribe((e) => {
        if (e.type === 'VERSION_READY') {
          this.toasts.info('Reload the page to use it.', 'A new version of the console is ready');
        }
      });
    }
  }
}
