import { ChangeDetectionStrategy, Component, computed, linkedSignal, signal } from '@angular/core';

import { RESUME_CONFIRMATION } from '../../api/halts.service';
import type { TicketLine } from '../../core/confirm/confirm.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { Sheet, TypedConfirm, typedMatches } from '../../shared/ui/sheet';

export interface ResumeRequest {
  /** Ticket lines: who it covers, what it stopped, since when. */
  lines: readonly TicketLine[];
  /** Any covered portfolio trades real money. */
  live: boolean;
}

interface Open extends ResumeRequest {
  resolve: (reason: string | null) => void;
}

/**
 * Resuming after a kill switch, as a ticket (UX-51): what starts again and
 * for whom, with the PAPER or LIVE stamp, a reason for the audit log and
 * the typed words RESUME TRADING. The page hosts one and calls `open()`,
 * which resolves to the reason, or null when cancelled.
 */
@Component({
  selector: 'app-resume-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet, TypedConfirm, ModeStamp],
  template: `
    <app-sheet
      [open]="!!current()"
      [wide]="true"
      labelledBy="resume-title"
      describedBy="resume-message"
      (dismiss)="answer(false)"
    >
      @if (current(); as req) {
        <form class="sheet-form" novalidate (submit)="$event.preventDefault(); answer(true)">
          <h2 id="resume-title">Resume trading?</h2>
          <div class="ticket" [class.live]="req.live" role="group" aria-label="Resume trading">
            <p class="ticket-head">
              <span class="ticket-kind">Resume trading</span>
              <app-mode-stamp [live]="req.live" />
            </p>
            <dl class="ticket-lines">
              @for (line of req.lines; track line.label) {
                <div>
                  <dt>{{ line.label }}</dt>
                  <dd>{{ line.value }}</dd>
                </div>
              }
            </dl>
          </div>
          <p id="resume-message" class="sheet-message">
            Turns off the kill switch. Orders go out again from the next trading run.
          </p>
          <div class="field">
            <label for="resume-reason">Reason</label>
            <textarea
              id="resume-reason"
              class="input"
              rows="2"
              maxlength="500"
              aria-describedby="resume-reason-hint"
              [attr.aria-invalid]="tried() && !reason().trim()"
              [value]="reason()"
              (input)="reason.set($any($event.target).value)"
            ></textarea>
            @if (tried() && !reason().trim()) {
              <p id="resume-reason-hint" class="error" role="alert">
                Say why it is safe to trade again.
              </p>
            } @else {
              <p id="resume-reason-hint" class="hint">Kept in the audit log.</p>
            }
          </div>
          <app-typed-confirm inputId="resume-typed" [phrase]="phrase" [(value)]="typed" />
          <div class="sheet-actions">
            <button type="button" class="btn" (click)="answer(false)">Cancel</button>
            <button type="submit" class="btn btn-danger" [disabled]="!canConfirm()">
              Resume trading
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
export class ResumeSheet {
  protected readonly phrase = RESUME_CONFIRMATION;
  protected readonly current = signal<Open | null>(null);
  protected readonly reason = linkedSignal({ source: this.current, computation: () => '' });
  protected readonly typed = linkedSignal({ source: this.current, computation: () => '' });
  protected readonly tried = linkedSignal({ source: this.current, computation: () => false });
  protected readonly canConfirm = computed(() => typedMatches(this.phrase, this.typed()));

  open(request: ResumeRequest): Promise<string | null> {
    this.current()?.resolve(null);
    return new Promise((resolve) => this.current.set({ ...request, resolve }));
  }

  protected answer(confirmed: boolean): void {
    const req = this.current();
    if (!req) return;
    if (confirmed) {
      this.tried.set(true);
      if (!this.canConfirm() || !this.reason().trim()) return;
    }
    const reason = this.reason().trim();
    this.current.set(null);
    req.resolve(confirmed ? reason : null);
  }
}
