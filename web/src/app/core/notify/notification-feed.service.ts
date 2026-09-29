import { DOCUMENT } from '@angular/common';
import { DestroyRef, Injectable, InjectionToken, inject, signal } from '@angular/core';

import { NotificationsService } from '../../api/notifications.service';

/** How often the bell re-reads the unread count while the tab is visible. 0 turns polling off. */
export const NOTIFICATION_POLL_MS = new InjectionToken<number>('NOTIFICATION_POLL_MS', {
  providedIn: 'root',
  factory: () => 60_000,
});

/** A deep link the router can open (same-app paths only), or null. */
export function appLink(link: string | null | undefined): string | null {
  return link && link.startsWith('/') && !link.startsWith('//') ? link : null;
}

/**
 * The unread notification count, shared by the bell (top bar and sidebar)
 * and the notifications page. Polls quietly (no error toasts) while the tab
 * is visible, pauses while it is hidden, and refreshes on return and after
 * any mark-read.
 */
@Injectable({ providedIn: 'root' })
export class NotificationFeedService {
  private readonly api = inject(NotificationsService);
  private readonly doc = inject(DOCUMENT);
  private readonly pollMs = inject(NOTIFICATION_POLL_MS);

  private readonly count = signal(0);
  private watchers = 0;
  /** Numbers each read; a count set after a read started wins over it. */
  private sequence = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly onVisibility = () => this.sync(true);

  /** Unread notifications from the last read. */
  readonly unread = this.count.asReadonly();

  /**
   * Keep the count fresh until `destroyRef` goes. Several watchers (two
   * bells) share one poll; it stops when the last one goes.
   */
  watch(destroyRef: DestroyRef): void {
    this.watchers += 1;
    if (this.watchers === 1) {
      this.doc.addEventListener('visibilitychange', this.onVisibility);
      this.sync(true);
    }
    destroyRef.onDestroy(() => {
      this.watchers -= 1;
      if (this.watchers > 0) return;
      this.doc.removeEventListener('visibilitychange', this.onVisibility);
      this.stopTimer();
    });
  }

  /** Re-read the unread count. A failed read keeps the last known count. */
  async refresh(): Promise<void> {
    const mine = ++this.sequence;
    try {
      const unread = (await this.api.feed({ limit: 1 }, true)).unread_count;
      // A newer read or a mark-read since this one started knows better.
      if (mine === this.sequence) this.count.set(unread);
    } catch {
      // Offline or signed out: keep what we had, say nothing.
    }
  }

  /** The page read the feed or marked items read: take the server's count. */
  set(unread: number): void {
    this.sequence++;
    this.count.set(Math.max(0, unread));
  }

  /** Mark these ids read, or everything without ids. Updates the count from the reply. */
  async markRead(ids?: number[]) {
    const result = await this.api.markRead(ids);
    this.set(result.unread_count);
    return result;
  }

  private visible(): boolean {
    return this.doc.visibilityState !== 'hidden';
  }

  /** Start or stop the timer to match the tab's visibility. */
  private sync(readNow: boolean): void {
    if (!this.visible()) {
      this.stopTimer();
      return;
    }
    if (readNow) void this.refresh();
    if (this.timer || this.pollMs <= 0) return;
    this.timer = setInterval(() => void this.refresh(), this.pollMs);
  }

  private stopTimer(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
