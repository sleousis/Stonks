import { DOCUMENT } from '@angular/common';
import { DestroyRef, Injectable, InjectionToken, inject, signal } from '@angular/core';

import { TicketsService } from '../../api/tickets.service';

/** How often the Approvals badge re-reads the waiting count while the tab is visible. 0 turns polling off. */
export const TICKET_POLL_MS = new InjectionToken<number>('TICKET_POLL_MS', {
  providedIn: 'root',
  factory: () => 60_000,
});

/**
 * How many order tickets wait for the signed-in person's approval, for
 * the badge on the Approvals nav item (roadmap 22.10). Reads the ticket
 * summary quietly (no error toasts) while the tab is visible, pauses while
 * it is hidden, and takes the count the approvals page already knows.
 */
@Injectable({ providedIn: 'root' })
export class TicketCountService {
  private readonly api = inject(TicketsService);
  private readonly doc = inject(DOCUMENT);
  private readonly pollMs = inject(TICKET_POLL_MS);

  private readonly count = signal(0);
  private watchers = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly onVisibility = () => this.sync(true);

  /** Tickets awaiting approval, from the last read. */
  readonly waiting = this.count.asReadonly();

  /** Keep the count fresh until `destroyRef` goes. Watchers share one poll. */
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

  /** Re-read the count. A failed read keeps the last known count. */
  async refresh(): Promise<void> {
    try {
      this.count.set((await this.api.summary(true)).awaiting_approval);
    } catch {
      // Offline or signed out: keep what we had, say nothing.
    }
  }

  /** The approvals page listed or decided tickets: take its count. */
  set(waiting: number): void {
    this.count.set(Math.max(0, waiting));
  }

  private sync(readNow: boolean): void {
    if (this.doc.visibilityState === 'hidden') {
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
