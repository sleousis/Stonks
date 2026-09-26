import { Injectable, signal } from '@angular/core';

export interface ConfirmOptions {
  title: string;
  /** What will happen, in plain words. */
  message: string;
  /** The action's verb, e.g. "Promote", "Run tick". Never "OK". */
  confirmLabel: string;
  cancelLabel?: string;
  /** `danger` colours the confirm button red (retire, delete, live actions). */
  tone?: 'default' | 'danger';
  /**
   * When set, the trader must type this exact text to enable the confirm
   * button. Required for ticks and promotions.
   */
  typedConfirmation?: string;
}

export interface ConfirmRequest extends ConfirmOptions {
  resolve: (confirmed: boolean) => void;
}

/**
 * Every mutating action goes through `confirm()` first. Rendered by
 * <app-confirm-dialog> in the shell.
 *
 *   if (!(await this.confirm.confirm({ title: 'Retire momentum-v3?', ... }))) return;
 */
@Injectable({ providedIn: 'root' })
export class ConfirmService {
  private readonly current = signal<ConfirmRequest | null>(null);
  readonly request = this.current.asReadonly();

  confirm(options: ConfirmOptions): Promise<boolean> {
    // A second request cancels the first rather than stacking dialogs.
    this.current()?.resolve(false);
    return new Promise<boolean>((resolve) => {
      this.current.set({
        ...options,
        resolve: (confirmed) => {
          this.current.set(null);
          resolve(confirmed);
        },
      });
    });
  }
}
