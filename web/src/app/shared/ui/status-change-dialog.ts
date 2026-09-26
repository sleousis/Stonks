import { ChangeDetectionStrategy, Component, computed, signal } from '@angular/core';

import type { GoLiveReport, StatusChangeRequest } from '../../api/models';
import { type CheckRow, checkRow } from '../golive-checks';
import { Sheet, TypedConfirm, typedMatches } from './sheet';
import { HoldButton } from './sheet-hold-button';
import { StatusPill } from './status-pill';

/** A promotion override needs a reason of at least this many characters (API rule). */
export const OVERRIDE_MIN_REASON = 20;

export interface StatusChangeOptions {
  title: string;
  /** What will happen, in plain words. */
  message: string;
  /** The action's verb ("Promote", "Retire"). */
  confirmLabel: string;
  tone?: 'default' | 'danger';
  /** Characters the reason needs (1 = required, 20 for an override). */
  minReason: number;
  reasonHint?: string;
  /** The trader must type this exact text to enable the confirm button. */
  typedConfirmation?: string;
  /**
   * Confirm by holding the button for about a second instead of typing
   * (gate-passing promotions). Ignored when `typedConfirmation` is set.
   */
  hold?: boolean;
  /** Ask the API to promote past a failing go-live gate. */
  override?: boolean;
  /** The go-live result to show first (promotions). */
  golive?: GoLiveReport | null;
  /** Why the go-live result is missing, e.g. the check failed to load. */
  goliveNote?: string | null;
}

interface Open extends StatusChangeOptions {
  resolve: (body: StatusChangeRequest | null) => void;
}

let nextId = 0;

/**
 * Asks for the reason behind a status change (promote, shadow, retire,
 * enable, disable) and, for promotions, shows the go-live result first.
 * Pages host one and call `open()`; it resolves to the request body, or
 * `null` when cancelled. Full-screen sheet on phones.
 *
 *   const body = await this.dialog().open({ title: 'Retire x?', minReason: 1, ... });
 *   if (body) await api.retire(id, body);
 */
@Component({
  selector: 'app-status-change-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill, Sheet, TypedConfirm, HoldButton],
  template: `
    <app-sheet
      [open]="!!current()"
      [wide]="true"
      [labelledBy]="id + '-title'"
      [describedBy]="id + '-message'"
      (dismiss)="answer(false)"
    >
      @if (current(); as req) {
        <form class="sheet-form" (submit)="$event.preventDefault(); answer(true)">
          <h2 [id]="id + '-title'">{{ req.title }}</h2>
          <p [id]="id + '-message'" class="sheet-message">{{ req.message }}</p>

          @if (req.golive; as g) {
            <div class="golive" [class.failed]="!g.passed" role="status">
              <p class="golive-head">
                <app-status-pill
                  [status]="g.passed ? 'pass' : 'fail'"
                  [label]="g.passed ? 'Go-live check passed' : 'Go-live check failed'"
                />
                <span class="golive-count">
                  @if (g.passed) {
                    All {{ g.checks.length }} checks passed.
                  } @else {
                    {{ failing().length }} of {{ g.checks.length }} checks failed.
                  }
                </span>
              </p>
              @if (failing().length) {
                <ul class="failing" aria-label="Failing go-live checks">
                  @for (c of failing(); track c.name) {
                    <li>
                      <span class="check-label">{{ c.label }}</span>
                      <span class="check-detail">{{ c.detail }}</span>
                    </li>
                  }
                </ul>
              }
            </div>
          } @else if (req.goliveNote) {
            <p class="note">{{ req.goliveNote }}</p>
          }

          <div class="field">
            <label [for]="id + '-reason'">
              {{ req.override ? 'Why override the gate?' : 'Reason' }}
            </label>
            <textarea
              class="input reason"
              rows="3"
              maxlength="1000"
              [id]="id + '-reason'"
              [attr.aria-describedby]="id + '-reason-hint'"
              [attr.aria-invalid]="showReasonError()"
              [value]="reason()"
              (input)="reason.set($any($event.target).value)"
            ></textarea>
            <span
              class="hint"
              [id]="id + '-reason-hint'"
              [class.error]="showReasonError()"
              aria-live="polite"
            >
              {{ reasonHint() }}
            </span>
          </div>

          @if (req.typedConfirmation) {
            <app-typed-confirm
              [inputId]="id + '-typed'"
              [phrase]="req.typedConfirmation"
              [(value)]="typed"
            />
          }

          <div class="sheet-actions">
            <button type="button" class="btn" (click)="answer(false)">Cancel</button>
            @if (holdMode()) {
              <app-hold-button
                [label]="req.confirmLabel"
                [tone]="req.tone ?? 'default'"
                [disabled]="!canConfirm()"
                (confirmed)="answer(true, true)"
              />
            } @else {
              <button
                type="submit"
                class="btn"
                [class.btn-danger]="req.tone === 'danger'"
                [class.btn-primary]="req.tone !== 'danger'"
                [disabled]="!canConfirm()"
              >
                {{ req.confirmLabel }}
              </button>
            }
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    .note {
      color: var(--color-ink-2);
    }
    .golive {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3);
      border-left: 3px solid var(--color-gain);
      border-radius: var(--radius-sm);
      background: var(--color-gain-soft);
    }
    .golive.failed {
      border-left-color: var(--color-loss);
      background: var(--color-loss-soft);
    }
    .golive-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-sm);
    }
    .failing {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
      font-size: var(--text-sm);
    }
    .failing li {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .check-label {
      font-weight: var(--weight-semibold);
    }
    .check-detail {
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .reason {
      min-height: 5rem;
      resize: vertical;
      font: inherit;
    }
    .hint.error {
      color: var(--color-loss);
    }
  `,
})
export class StatusChangeDialog {
  protected readonly id = `status-change-${nextId++}`;

  protected readonly current = signal<Open | null>(null);
  protected readonly reason = signal('');
  protected readonly typed = signal('');
  private readonly tried = signal(false);

  protected readonly failing = computed<CheckRow[]>(() => {
    const g = this.current()?.golive;
    return g ? g.checks.filter((c) => !c.passed).map(checkRow) : [];
  });

  private readonly reasonLength = computed(() => this.reason().trim().length);
  private readonly reasonOk = computed(
    () => this.reasonLength() >= (this.current()?.minReason ?? 1),
  );
  protected readonly showReasonError = computed(() => this.tried() && !this.reasonOk());

  protected readonly reasonHint = computed(() => {
    const req = this.current();
    if (!req) return '';
    const min = req.minReason;
    if (min > 1) {
      const left = Math.max(0, min - this.reasonLength());
      return left
        ? `At least ${min} characters, ${left} more to go. It is kept in the audit history.`
        : 'Kept in the audit history with your override.';
    }
    if (this.showReasonError()) return 'Enter a reason to continue.';
    return req.reasonHint ?? 'Kept in the status history so others can see why.';
  });

  protected readonly canConfirm = computed(() => {
    const req = this.current();
    if (!req) return false;
    return typedMatches(req.typedConfirmation, this.typed()) && this.reasonOk();
  });

  /** Hold to confirm, unless the trader must type something. */
  protected readonly holdMode = computed(() => {
    const req = this.current();
    return !!req?.hold && !req.typedConfirmation;
  });

  /** Resolves to the request body, or `null` when the trader cancels. */
  open(options: StatusChangeOptions): Promise<StatusChangeRequest | null> {
    this.current()?.resolve(null);
    this.reason.set('');
    this.typed.set('');
    this.tried.set(false);
    return new Promise((resolve) => {
      this.current.set({
        ...options,
        resolve: (body) => {
          this.current.set(null);
          resolve(body);
        },
      });
    });
  }

  protected answer(confirmed: boolean, fromHold = false): void {
    const req = this.current();
    if (!req) return;
    if (!confirmed) {
      req.resolve(null);
      return;
    }
    this.tried.set(true);
    // Enter in a field submits the form: in hold mode only the hold confirms.
    if (!this.canConfirm() || (this.holdMode() && !fromHold)) return;
    req.resolve({ reason: this.reason().trim(), override: !!req.override });
  }
}
