import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  model,
  signal,
} from '@angular/core';

import { HaltsService } from '../../api/halts.service';
import type { KillSwitchRequest } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { killTicket } from '../../core/halts/kill-ticket';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ModeStamp } from './mode-stamp';
import { Sheet } from './sheet';

type KillScope = KillSwitchRequest['scope'];

/** Filled in so a trader in a hurry can stop with one more tap. The audit log records who. */
export const DEFAULT_KILL_REASON = 'Stopped from the console.';

/**
 * The Stop trading sheet (UX-01): the kill switch in two taps from any
 * page. Scope starts on the portfolio the trader is looking at, the reason
 * is filled in, and the sheet itself is the ticket: scope, what stops, what
 * still goes out, the reason and the PAPER or LIVE stamp, then one red
 * button. Resuming stays on the halts page, behind the typed words.
 */
@Component({
  selector: 'app-kill-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet, ModeStamp],
  template: `
    <app-sheet
      [open]="open()"
      [wide]="true"
      labelledBy="kill-sheet-title"
      describedBy="kill-sheet-message"
      (dismiss)="close()"
    >
      @if (open()) {
        <form class="sheet-form kill-form" novalidate (submit)="$event.preventDefault(); submit()">
          <h2 id="kill-sheet-title">Stop trading</h2>
          <p id="kill-sheet-message" class="sheet-message">
            New orders stop at once. No position is closed. You can resume on the Halts page.
          </p>

          <fieldset class="choices">
            <legend>Stop for</legend>
            @if (picked(); as p) {
              <label class="check">
                <input
                  type="radio"
                  name="kill-sheet-scope"
                  value="portfolio"
                  [checked]="scope() === 'portfolio'"
                  (change)="scope.set('portfolio')"
                />
                <span>{{ p.name }}</span>
              </label>
            }
            <label class="check">
              <input
                type="radio"
                name="kill-sheet-scope"
                value="user"
                [checked]="scope() === 'user'"
                (change)="scope.set('user')"
              />
              <span>All your portfolios</span>
            </label>
            @if (canGlobal()) {
              <label class="check">
                <input
                  type="radio"
                  name="kill-sheet-scope"
                  value="global"
                  [checked]="scope() === 'global'"
                  (change)="scope.set('global')"
                />
                <span>Every portfolio, for every trader</span>
              </label>
            }
          </fieldset>

          <fieldset class="choices">
            <legend>What stops</legend>
            <label class="check">
              <input
                type="radio"
                name="kill-sheet-stops"
                value="all"
                [checked]="!buysOnly()"
                (change)="buysOnly.set(false)"
              />
              <span>All new orders</span>
            </label>
            <label class="check">
              <input
                type="radio"
                name="kill-sheet-stops"
                value="buys"
                [checked]="buysOnly()"
                (change)="buysOnly.set(true)"
              />
              <span
                >Stop new buys only <span class="muted">(sells and exits still go out)</span></span
              >
            </label>
          </fieldset>

          <div class="field">
            <label for="kill-sheet-reason">Reason</label>
            <input
              id="kill-sheet-reason"
              class="input"
              maxlength="500"
              aria-describedby="kill-sheet-reason-hint"
              [attr.aria-invalid]="!!reasonError()"
              [value]="reason()"
              (input)="reason.set($any($event.target).value)"
            />
            @if (reasonError(); as e) {
              <p id="kill-sheet-reason-hint" class="error" role="alert">{{ e }}</p>
            } @else {
              <p id="kill-sheet-reason-hint" class="hint">
                Kept in the audit log. Edit it if you like.
              </p>
            }
          </div>

          <div class="ticket" [class.live]="ticket().live" role="group" aria-label="Kill switch">
            <p class="ticket-head">
              <span class="ticket-kind">Kill switch</span>
              <app-mode-stamp [live]="ticket().live" />
            </p>
            <dl class="ticket-lines">
              @for (line of ticket().lines; track line.label) {
                <div>
                  <dt>{{ line.label }}</dt>
                  <dd>{{ line.value }}</dd>
                </div>
              }
            </dl>
          </div>

          <div class="sheet-actions">
            <button type="button" class="btn" (click)="close()">Cancel</button>
            <button
              type="submit"
              class="btn btn-danger"
              [disabled]="busy()"
              [attr.aria-busy]="busy()"
            >
              {{ busy() ? 'Stopping…' : 'Stop trading' }}
            </button>
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    .choices {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
      margin: 0;
      padding: 0;
      border: 0;
    }
    legend {
      padding: 0;
      margin-bottom: var(--space-1);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .check {
      align-items: flex-start;
      padding-block: var(--space-1);
    }
    .check input {
      flex: none;
      margin-top: 3px;
    }
    .check span {
      overflow-wrap: anywhere;
    }
  `,
})
export class KillSheet {
  /** Two-way: the strip opens it, the sheet closes itself. */
  readonly open = model(false);

  private readonly api = inject(HaltsService);
  private readonly state = inject(HaltStateService);
  private readonly portfolios = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  protected readonly canGlobal = computed(() => this.session.can('killswitch.global'));
  /** The portfolio the trader is looking at (the picker's choice or their default). */
  protected readonly picked = this.portfolios.current;

  /** Starts on the picked portfolio each time the sheet opens. */
  protected readonly scope = linkedSignal<{ open: boolean; id: string | null }, KillScope>({
    source: () => ({ open: this.open(), id: this.picked()?.id ?? null }),
    computation: ({ id }) => (id ? 'portfolio' : 'user'),
  });
  protected readonly buysOnly = linkedSignal({ source: this.open, computation: () => false });
  protected readonly reason = linkedSignal({
    source: this.open,
    computation: () => DEFAULT_KILL_REASON,
  });
  protected readonly busy = signal(false);

  protected readonly reasonError = computed(() =>
    this.reason().trim() ? null : 'Say why in a few words. Others read it before they resume.',
  );

  private readonly scopeText = computed(() => {
    const name = this.state.scopeText();
    const scope = this.scope();
    if (scope === 'portfolio') {
      return name({ scope, portfolio_id: this.picked()?.id ?? null, user_id: null });
    }
    return name({ scope, portfolio_id: null, user_id: null });
  });

  /** Real money when the covered portfolio (or any of the trader's) trades live. */
  private readonly live = computed(() =>
    this.scope() === 'portfolio'
      ? this.picked()?.trading === 'live'
      : this.portfolios.options().some((p) => p.trading === 'live'),
  );

  protected readonly ticket = computed(() =>
    killTicket({
      scopeText: this.scopeText(),
      buysOnly: this.buysOnly(),
      reason: this.reason(),
      live: this.live(),
    }),
  );

  protected close(): void {
    if (!this.busy()) this.open.set(false);
  }

  protected async submit(): Promise<void> {
    if (this.busy() || this.reasonError()) return;
    const scope = this.scope();
    const body: KillSwitchRequest = {
      scope,
      reason: this.reason().trim(),
      buys_only: this.buysOnly(),
      portfolio_id: scope === 'portfolio' ? (this.picked()?.id ?? null) : null,
    };
    const target = this.scopeText();
    this.busy.set(true);
    try {
      const halt = await this.api.kill(body);
      this.state.add(halt);
      this.toasts.success(
        body.buys_only ? `${target}: new buys are stopped.` : `${target}: new orders are stopped.`,
        'Kill switch on',
      );
      this.busy.set(false);
      this.open.set(false);
      void this.state.refresh();
    } catch {
      // The error interceptor already showed the API's message.
      this.busy.set(false);
    }
  }
}
