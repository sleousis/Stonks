import { DOCUMENT } from '@angular/common';
import { DestroyRef, Injectable, InjectionToken, computed, inject, signal } from '@angular/core';

import { OrderDraftsService } from '../../api/order-drafts.service';
import { TicketsService } from '../../api/tickets.service';

/** How often the Approvals badge re-reads the waiting count while the tab is visible. 0 turns polling off. */
export const TICKET_POLL_MS = new InjectionToken<number>('TICKET_POLL_MS', {
  providedIn: 'root',
  factory: () => 60_000,
});

/**
 * How many orders wait for the signed-in person's approval, for the badge
 * on the Approvals nav item (roadmap 22.10, F9): strategy tickets plus
 * suggested orders, the two kinds the one Approvals inbox holds. Reads both
 * quietly (no error toasts) while the tab is visible, pauses while it is
 * hidden, and takes the counts the approvals page already knows.
 */
@Injectable({ providedIn: 'root' })
export class TicketCountService {
  private readonly api = inject(TicketsService);
  private readonly drafts = inject(OrderDraftsService);
  private readonly doc = inject(DOCUMENT);
  private readonly pollMs = inject(TICKET_POLL_MS);

  private readonly tickets = signal(0);
  private readonly suggested = signal(0);
  private watchers = 0;
  /** Numbers each read; counts set after a read started win over it. */
  private sequence = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly onVisibility = () => this.sync(true);

  /** Tickets and suggested orders awaiting approval, from the last read. */
  readonly waiting = computed(() => this.tickets() + this.suggested());

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

  /** Re-read both counts. A failed read keeps that part's last known count. */
  async refresh(): Promise<void> {
    const mine = ++this.sequence;
    const [tickets, suggested] = await Promise.allSettled([
      this.api.summary(true),
      this.drafts.pendingCount(),
    ]);
    // A newer read, or counts the approvals page set since, know better.
    if (mine !== this.sequence) return;
    // Offline or signed out: keep what we had, say nothing.
    if (tickets.status === 'fulfilled') this.tickets.set(tickets.value.awaiting_approval);
    if (suggested.status === 'fulfilled') this.suggested.set(suggested.value);
  }

  /**
   * The approvals page listed or decided orders: take its counts. One
   * argument is the whole count (older callers); two split it.
   */
  set(tickets: number, suggested = 0): void {
    this.sequence++;
    this.tickets.set(Math.max(0, tickets));
    this.suggested.set(Math.max(0, suggested));
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
