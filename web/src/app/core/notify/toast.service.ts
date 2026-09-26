import { Injectable, signal } from '@angular/core';

export type ToastTone = 'success' | 'info' | 'error';

export interface Toast {
  id: number;
  tone: ToastTone;
  title?: string;
  message: string;
}

const DURATION_MS: Record<ToastTone, number> = { success: 5000, info: 6000, error: 10000 };
const MAX_TOASTS = 4;

/**
 * Transient notifications, rendered by <app-toast-outlet> in the shell.
 * Name the outcome with the action's verb: "Promoted momentum-v3".
 */
@Injectable({ providedIn: 'root' })
export class ToastService {
  private nextId = 1;
  private readonly timers = new Map<number, ReturnType<typeof setTimeout>>();
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
    clearTimeout(this.timers.get(id));
    this.timers.delete(id);
    this.items.update((list) => list.filter((t) => t.id !== id));
  }

  private show(tone: ToastTone, message: string, title?: string): number {
    // Drop an identical toast already on screen (e.g. the same failure twice).
    const dup = this.items().find((t) => t.message === message && t.tone === tone);
    if (dup) this.dismiss(dup.id);
    const id = this.nextId++;
    this.items.update((list) => [...list, { id, tone, title, message }].slice(-MAX_TOASTS));
    this.timers.set(
      id,
      setTimeout(() => this.dismiss(id), DURATION_MS[tone]),
    );
    return id;
  }
}
