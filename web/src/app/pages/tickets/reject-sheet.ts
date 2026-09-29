import { ChangeDetectionStrategy, Component, linkedSignal, signal } from '@angular/core';

import { Sheet } from '../../shared/ui/sheet';

interface Open {
  /** "Buy 5 AAPL.US", shown in the title. */
  what: string;
  /** The ticket's portfolio trades real money: only then a red button. */
  live: boolean;
  resolve: (reason: string | null) => void;
}

/** Shortest reason the API takes. */
export const MIN_REASON = 3;

/**
 * Rejecting a ticket asks for a reason, kept in the audit log. The page
 * hosts one and calls `open()`, which resolves to the reason, or null when
 * cancelled. A full-screen sheet on phones.
 */
@Component({
  selector: 'app-reject-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet],
  template: `
    <app-sheet
      [open]="!!current()"
      labelledBy="reject-title"
      describedBy="reject-message"
      (dismiss)="answer(false)"
    >
      @if (current(); as req) {
        <form class="sheet-form" novalidate (submit)="$event.preventDefault(); answer(true)">
          <h2 id="reject-title">Reject {{ req.what }}?</h2>
          <p id="reject-message" class="sheet-message">
            Nothing goes to your broker for this ticket. The next trading run decides again.
          </p>
          <div class="field">
            <label for="reject-reason">Reason</label>
            <textarea
              id="reject-reason"
              class="input"
              rows="2"
              maxlength="500"
              aria-describedby="reject-reason-hint"
              [attr.aria-invalid]="tried() && short()"
              [value]="reason()"
              (input)="reason.set($any($event.target).value)"
            ></textarea>
            @if (tried() && short()) {
              <p id="reject-reason-hint" class="error" role="alert">Say why, in a few words.</p>
            } @else {
              <p id="reject-reason-hint" class="hint">Kept in the audit log.</p>
            }
          </div>
          <div class="sheet-actions">
            <button type="button" class="btn" (click)="answer(false)">Keep it</button>
            <button
              type="submit"
              class="btn"
              [class.btn-danger]="req.live"
              [class.btn-primary]="!req.live"
            >
              Reject
            </button>
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    textarea.input {
      min-height: 4.5rem;
      padding: var(--space-2) var(--space-3);
      resize: vertical;
    }
  `,
})
export class RejectSheet {
  protected readonly current = signal<Open | null>(null);
  protected readonly reason = linkedSignal({ source: this.current, computation: () => '' });
  protected readonly tried = linkedSignal({ source: this.current, computation: () => false });

  open(what: string, live = true): Promise<string | null> {
    this.current()?.resolve(null);
    return new Promise((resolve) => this.current.set({ what, live, resolve }));
  }

  protected short(): boolean {
    return this.reason().trim().length < MIN_REASON;
  }

  protected answer(confirmed: boolean): void {
    const req = this.current();
    if (!req) return;
    if (confirmed) {
      this.tried.set(true);
      if (this.short()) return;
    }
    const reason = this.reason().trim();
    this.current.set(null);
    req.resolve(confirmed ? reason : null);
  }
}
