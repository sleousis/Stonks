import { Injectable, signal } from '@angular/core';

export type ToastTone = 'success' | 'info' | 'error';

export interface Toast {
  id: number;
  tone: ToastTone;
  title?: string;
  message: string;
}

/** Why a toast's timer is held: the pointer is over it, or focus is inside it. */
export type ToastHold = 'hover' | 'focus';

/** How long a toast stays. Errors have none: they stay until dismissed. */
const DURATION_MS: Record<ToastTone, number | null> = {
  success: 5000,
  info: 6000,
  error: null,
};
const MAX_TOASTS = 4;

interface Timer {
  handle: ReturnType<typeof setTimeout> | null;
  /** Time left when paused, or the full duration before the first start. */
  remaining: number;
  startedAt: number;
  holds: Set<ToastHold>;
}

/**
 * Transient notifications, rendered by <app-toast-outlet> in the shell.
 * Name the outcome with the action's verb: "Promoted momentum-v3".
 *
 * Success and info toasts go after a few seconds; the countdown pauses
 * while the pointer is over a toast or focus is inside it, so a toast being
 * read never vanishes. Errors stay until the trader dismisses them.
 */
@Injectable({ providedIn: 'root' })
export class ToastService {
  private nextId = 1;
  private readonly timers = new Map<number, Timer>();
  private readonly items = signal<readonly Toast[]>([]);
  readonly toasts = this.items.asReadonly();

  success(message: string, title?: string): number {
    return this.show('success', message, title);
  }

  info(message: string, title?: string): number {
    return this.show('info', message, title);
  }

  error(message: string, title?: string): number {
    return this.show('error', message, title);
  }

  dismiss(id: number): void {
    const timer = this.timers.get(id);
    if (timer?.handle) clearTimeout(timer.handle);
    this.timers.delete(id);
    this.items.update((list) => list.filter((t) => t.id !== id));
  }

  /** Stop a toast's countdown (pointer over it, or focus inside it). */
  pause(id: number, why: ToastHold): void {
    const timer = this.timers.get(id);
    if (!timer) return;
    timer.holds.add(why);
    if (timer.handle) {
      clearTimeout(timer.handle);
      timer.handle = null;
      timer.remaining = Math.max(0, timer.remaining - (Date.now() - timer.startedAt));
    }
  }

  /** Let the countdown carry on once nothing holds the toast any more. */
  resume(id: number, why: ToastHold): void {
    const timer = this.timers.get(id);
    if (!timer) return;
    timer.holds.delete(why);
    if (timer.holds.size === 0 && !timer.handle) this.start(id, timer);
  }

  private show(tone: ToastTone, message: string, title?: string): number {
    // Drop an identical toast already on screen (e.g. the same failure twice).
    const dup = this.items().find((t) => t.message === message && t.tone === tone);
    if (dup) this.dismiss(dup.id);
    const id = this.nextId++;
    const next = [...this.items(), { id, tone, title, message }];
    // Over the limit: the oldest go, and their timers with them.
    for (const old of next.slice(0, Math.max(0, next.length - MAX_TOASTS))) {
      this.dismiss(old.id);
    }
    this.items.set(next.slice(-MAX_TOASTS));
    const duration = DURATION_MS[tone];
    if (duration !== null) {
      const timer: Timer = { handle: null, remaining: duration, startedAt: 0, holds: new Set() };
      this.timers.set(id, timer);
      this.start(id, timer);
    }
    return id;
  }

  private start(id: number, timer: Timer): void {
    timer.startedAt = Date.now();
    timer.handle = setTimeout(() => this.dismiss(id), timer.remaining);
  }
}
