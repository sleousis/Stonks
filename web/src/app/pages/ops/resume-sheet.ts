import { ChangeDetectionStrategy, Component, computed, linkedSignal, signal } from '@angular/core';

import { RESUME_CONFIRMATION } from '../../api/halts.service';
import type { ResumeChecksView } from '../../api/models';
import type { TicketLine } from '../../core/confirm/confirm.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { Sheet, TypedConfirm, typedMatches } from '../../shared/ui/sheet';

export interface ResumeRequest {
  /** Ticket lines: who it covers, what it stopped, since when. */
  lines: readonly TicketLine[];
  /** Any covered portfolio trades real money. */
  live: boolean;
}

/** What the person confirmed: the reason, and whether they resume past a failed check. */
export interface ResumeAnswer {
  reason: string;
  overrideChecks: boolean;
}

/** The resume checks: still loading, could not be read, or the answer. */
export type ChecksState = 'loading' | 'error' | ResumeChecksView;

interface Open extends ResumeRequest {
  resolve: (answer: ResumeAnswer | null) => void;
}

const CHECK_LABEL: Record<string, string> = {
  broker: 'Broker',
  gateway_up: 'Gateway up',
  last_reconcile_clean: 'Last reconcile clean',
  account_readable: 'Account readable',
  equity_cover: 'Equity covers the largest position',
};

/**
 * Resuming after Stop trading, as a ticket (UX-51): what starts again and
 * for whom, with the PAPER or LIVE stamp, a reason for the audit log and
 * the typed words RESUME TRADING. Above the phrase it shows the resume
 * checks (roadmap 23.15): the gateway, the last reconcile, the account and
 * the equity cover. A failed check needs a ticked override. The confirm
 * button is red only at a real money stage, the primary button on paper. The page hosts
 * one, calls `open()` and then `setChecks()`. `open()` resolves to the
 * answer, or null when cancelled.
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
            Ends Stop trading. Orders go out again from the next trading run.
          </p>
          <section class="checks" aria-labelledby="resume-checks-title">
            <h3 id="resume-checks-title">Checks before resuming</h3>
            @switch (stateKind()) {
              @case ('loading') {
                <p class="hint" role="status">Checking the broker...</p>
              }
              @case ('error') {
                <p class="hint" role="status">
                  The checks could not be read. The server runs them again when you resume.
                </p>
              }
              @default {
                @if (checkList().length === 0) {
                  <p class="hint">No portfolio here trades at a real broker. Nothing to check.</p>
                } @else {
                  <ul class="check-list">
                    @for (c of checkList(); track c.name + (c.portfolio_id ?? '')) {
                      <li [class.fail]="c.passed === false" [class.ok]="c.passed === true">
                        <span class="check-mark" aria-hidden="true">{{ mark(c.passed) }}</span>
                        <span class="visually-hidden">{{ spoken(c.passed) }}:</span>
                        <strong>{{ label(c.name) }}</strong>
                        @if (c.portfolio_id) {
                          <span class="muted">{{ c.portfolio_id }}</span>
                        }
                        <span class="check-detail">{{ c.detail }}</span>
                      </li>
                    }
                  </ul>
                }
                @if (failed()) {
                  <label class="override" for="resume-override">
                    <input
                      id="resume-override"
                      type="checkbox"
                      [checked]="override()"
                      (change)="override.set($any($event.target).checked)"
                    />
                    Resume anyway. I know why these checks failed (kept in the audit log).
                  </label>
                }
              }
            }
          </section>
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
            <!-- Red only when real money moves again; paper resumes with the primary button. -->
            <button
              type="submit"
              class="btn"
              [class.btn-danger]="req.live"
              [class.btn-primary]="!req.live"
              [disabled]="!canConfirm()"
            >
              Resume trading
            </button>
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    .checks h3 {
      margin: 0 0 var(--space-2);
      font-size: var(--text-md);
    }
    .check-list {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .check-list li {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      align-items: baseline;
    }
    .check-mark {
      min-width: 2.5rem;
      font-weight: 600;
    }
    .check-list li.ok .check-mark {
      color: var(--color-gain);
    }
    .check-list li.fail .check-mark {
      color: var(--color-loss);
    }
    .check-detail {
      flex-basis: 100%;
      color: var(--color-ink-3);
      font-size: var(--text-sm);
    }
    .override {
      display: flex;
      gap: var(--space-2);
      align-items: center;
      min-height: 44px;
      margin-top: var(--space-2);
    }
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
  protected readonly checks = linkedSignal<Open | null, ChecksState>({
    source: this.current,
    computation: () => 'loading',
  });
  protected readonly override = linkedSignal({ source: this.current, computation: () => false });
  protected readonly stateKind = computed(() => {
    const state = this.checks();
    return typeof state === 'object' ? 'ready' : state;
  });
  protected readonly checkList = computed(() => {
    const state = this.checks();
    return typeof state === 'object' ? state.checks : [];
  });
  protected readonly failed = computed(() => this.checkList().some((c) => c.passed === false));
  protected readonly canConfirm = computed(
    () => typedMatches(this.phrase, this.typed()) && (!this.failed() || this.override()),
  );

  open(request: ResumeRequest): Promise<ResumeAnswer | null> {
    this.current()?.resolve(null);
    return new Promise((resolve) => this.current.set({ ...request, resolve }));
  }

  /** The resume checks once read, or 'error' when they could not be. */
  setChecks(state: ChecksState): void {
    if (this.current()) this.checks.set(state);
  }

  protected label(name: string): string {
    return CHECK_LABEL[name] ?? name;
  }

  protected mark(passed: boolean | null | undefined): string {
    return passed === true ? 'OK' : passed === false ? 'FAIL' : '?';
  }

  protected spoken(passed: boolean | null | undefined): string {
    return passed === true ? 'Passed' : passed === false ? 'Failed' : 'Unknown';
  }

  protected answer(confirmed: boolean): void {
    const req = this.current();
    if (!req) return;
    if (confirmed) {
      this.tried.set(true);
      if (!this.canConfirm() || !this.reason().trim()) return;
    }
    const reason = this.reason().trim();
    const overrideChecks = this.failed() && this.override();
    this.current.set(null);
    req.resolve(confirmed ? { reason, overrideChecks } : null);
  }
}
