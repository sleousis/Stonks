import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';

import {
  type SubscriptionMode,
  type SubscriptionUpdate,
  type SubscriptionView,
  SubscriptionsService,
} from '../../api/subscriptions.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { MODES, modeLabel } from '../governance-labels';
import { strategyDisplayName } from '../strategy-names';
import { HelpTip } from '../ui/help-tip';
import { autoBlockedReason, isGatedMode } from './follow-rules';

let nextId = 0;

/**
 * How you follow one strategy (M8, UX-31): an on and off switch and the four
 * follow modes (Alerts only, Paper, Approve each trade, Automatic) in one
 * compact row, the current mode's line, and why the gated modes are closed.
 * The same control on Today and on the strategy page.
 *
 * Approve each trade and Automatic ask for a fresh code and show a ticket
 * with the portfolio's PAPER or LIVE stamp. Automatic also asks you to type
 * the strategy's name as you see it (never a hidden id). Whether real money
 * moves depends on the portfolio's stage, so the words follow the stamp.
 *
 * Project the strategy's name (a link, a status pill) into the head.
 *
 *   <app-follow-control [sub]="s" (changed)="replace($event)">
 *     <a [routerLink]="['/strategies', s.strategy_id]">{{ name }}</a>
 *   </app-follow-control>
 */
@Component({
  selector: 'app-follow-control',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HelpTip],
  template: `
    <div class="head">
      <div class="name"><ng-content /></div>
      <button
        type="button"
        role="switch"
        class="switch"
        [attr.aria-checked]="current().enabled"
        [attr.aria-label]="'Follow ' + name()"
        [disabled]="busy() || !canManage()"
        (click)="toggle()"
      >
        <span class="track" aria-hidden="true"><span class="thumb"></span></span>
        <span class="state">{{ current().enabled ? 'On' : 'Off' }}</span>
      </button>
    </div>

    <fieldset class="modes" [disabled]="!current().enabled || !canManage()">
      <legend class="visually-hidden">How you follow {{ name() }}</legend>
      @for (m of modes; track m.value) {
        <label
          class="mode"
          [class.on]="current().mode === m.value"
          [class.locked]="locked(m.value)"
        >
          <input
            type="radio"
            [name]="id + '-mode'"
            [value]="m.value"
            [checked]="current().mode === m.value"
            [disabled]="busy() || (gated(m.value) && !canAuto()) || locked(m.value)"
            [attr.aria-describedby]="gated(m.value) && blocked() ? id + '-why' : null"
            (change)="setMode(m.value)"
          />
          {{ m.label }}
        </label>
      }
    </fieldset>

    @if (current().paused_reason) {
      <p class="note">Automatic is paused. {{ current().paused_reason }}</p>
    }
    @if (showHelp()) {
      <p class="help">{{ help() }} <app-help-tip [term]="modeWord()" /></p>
    }
    @if (blocked() && !gated(current().mode)) {
      <p class="why" [id]="id + '-why'">{{ blocked() }}</p>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-2);
      min-width: 0;
    }
    .head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-3);
    }
    .name {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      min-width: 0;
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .switch {
      display: inline-flex;
      flex: none;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--touch-min);
      padding: 0 var(--space-1);
      border: 0;
      background: none;
      color: inherit;
      cursor: pointer;
      font-size: var(--text-sm);
    }
    .switch:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    .track {
      position: relative;
      width: 40px;
      height: 24px;
      border: 1px solid var(--color-control-border);
      border-radius: 12px;
      background: var(--color-surface-3);
      transition: background-color var(--dur-fast) var(--ease);
    }
    .thumb {
      position: absolute;
      top: 2px;
      left: 2px;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: var(--color-surface);
      box-shadow: var(--shadow-1);
      transition: transform var(--dur-fast) var(--ease);
    }
    .switch[aria-checked='true'] .track {
      background: var(--color-primary);
      border-color: var(--color-primary);
    }
    .switch[aria-checked='true'] .thumb {
      transform: translateX(16px);
    }
    .state {
      min-width: 2em;
    }
    /* One row when there is room, two by two on a phone. The 1px gap on a
       border-coloured ground draws the dividers however the modes wrap. */
    .modes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(max(6.5rem, 22%), 1fr));
      gap: 1px;
      margin: 0;
      padding: 0;
      border: 1px solid var(--color-border-strong);
      border-radius: var(--radius-sm);
      background: var(--color-border-strong);
      overflow: hidden;
    }
    .modes:disabled {
      opacity: 0.55;
    }
    .mode {
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: var(--touch-min);
      padding: 0 var(--space-2);
      background: var(--color-surface);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
      text-align: center;
      cursor: pointer;
    }
    .mode input {
      position: absolute;
      inset: 0;
      margin: 0;
      opacity: 0;
      cursor: inherit;
    }
    .mode:has(input:focus-visible) {
      outline: 2px solid var(--color-focus);
      outline-offset: -2px;
    }
    .mode.on {
      background: var(--color-primary);
      color: var(--color-primary-ink);
      font-weight: var(--weight-semibold);
    }
    .mode.locked {
      color: var(--color-ink-3);
      cursor: not-allowed;
    }
    .help,
    .why {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .why {
      color: var(--color-ink-3);
    }
    .note {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
    }
    @media (max-width: 30rem) {
      .modes {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
  `,
})
export class FollowControl {
  /** The follow to show and change. */
  readonly sub = input.required<SubscriptionView>();
  /** The strategy's name as people read it; defaults to the display name of its id. */
  readonly strategyName = input<string | null>(null);
  /** One line on what the current mode does, with its glossary tip. */
  readonly showHelp = input(true);
  /**
   * In a list that says the unlock rule once above it (Today): the closed
   * modes' line says only how far this follow has come ("Paper days: 4 of 20.").
   */
  readonly compact = input(false);
  /** Each change the server accepted, with the updated follow. */
  readonly changed = output<SubscriptionView>();

  private readonly api = inject(SubscriptionsService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly portfolios = inject(PortfolioContextService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  protected readonly id = `follow-${nextId++}`;
  protected readonly modes = MODES;
  /** The follow as shown, updated in place from each change's answer. */
  protected readonly current = linkedSignal(() => this.sub());
  protected readonly busy = signal(false);

  /** Switching on or off and changing modes. */
  protected readonly canManage = computed(() => this.session.can('portfolio.manage'));
  /** Automatic trades without asking: its own permission. */
  protected readonly canAuto = computed(() => this.session.can('subscription.auto_enable'));

  protected readonly name = computed(
    () => this.strategyName()?.trim() || strategyDisplayName(this.current().strategy_id),
  );
  protected readonly blocked = computed(() =>
    autoBlockedReason(this.current(), { compact: this.compact() }),
  );
  protected readonly modeWord = computed(() => modeLabel(this.current().mode));
  protected readonly help = computed(
    () => MODES.find((m) => m.value === this.current().mode)?.help ?? '',
  );

  protected gated(mode: string): boolean {
    return isGatedMode(mode);
  }

  /** A gated mode the auto gate still keeps closed. */
  protected locked(mode: string): boolean {
    return isGatedMode(mode) && !!this.blocked() && !isGatedMode(this.current().mode);
  }

  protected async toggle(): Promise<void> {
    const sub = this.current();
    if (this.busy()) return;
    const on = !sub.enabled;
    // Switching an automatic follow back on restarts its trades (UX-02).
    if (on && sub.mode === 'auto' && !(await this.confirmAuto(true))) return;
    await this.change({ enabled: on }, `${this.name()} is ${on ? 'on' : 'off'}.`);
  }

  protected async setMode(mode: SubscriptionMode): Promise<void> {
    const sub = this.current();
    if (mode === sub.mode) return;
    const confirmed =
      mode === 'auto'
        ? await this.confirmAuto(false)
        : mode === 'approve'
          ? await this.confirmApprove()
          : true;
    if (!confirmed) {
      this.revert();
      return;
    }
    await this.change({ mode }, `You follow ${this.name()} as ${modeLabel(mode)} now.`);
  }

  /** The portfolio this follow trades, and whether it trades real money. */
  private book() {
    const sub = this.current();
    const book = this.portfolios.options().find((p) => p.id === sub.portfolio_id) ?? null;
    // Unknown portfolio: assume real money, so the ticket errs on the safe side.
    return { name: book?.name ?? 'Your portfolio', live: book ? book.trading === 'live' : true };
  }

  /** A fresh code, then a ticket with the strategy's name typed (UX-02, UX-14). */
  private async confirmAuto(again: boolean): Promise<boolean> {
    const name = this.name();
    const verb = again ? 'Turn Automatic back on' : 'Turn on Automatic';
    if (!(await this.stepUp.ensure(`${verb} for ${name}.`))) return false;
    const book = this.book();
    return this.confirm.confirm({
      title: `${verb} for ${name}?`,
      message: book.live
        ? `Stonks sends real orders to your broker for this strategy on every trading run, without asking each time. You can switch back to Paper at any time.`
        : `Stonks places paper orders in ${book.name} for this strategy on every trading run, without asking each time. No real money moves.`,
      confirmLabel: verb,
      tone: book.live ? 'danger' : 'default',
      typedConfirmation: name,
      ticket: {
        live: book.live,
        lines: [
          { label: 'Strategy', value: name },
          { label: 'Portfolio', value: book.name },
          { label: 'Follow', value: modeLabel('auto') },
        ],
      },
    });
  }

  /** A fresh code, then a ticket (no typed name: every trade still waits for you). */
  private async confirmApprove(): Promise<boolean> {
    const name = this.name();
    if (!(await this.stepUp.ensure(`Approve each trade for ${name}.`))) return false;
    const book = this.book();
    return this.confirm.confirm({
      title: `Approve each trade for ${name}?`,
      message:
        'After each trading run, the trades this strategy wants wait for you under Approvals. ' +
        (book.live
          ? 'Nothing goes to your broker until you approve it with a code. '
          : 'Nothing is placed in your paper portfolio until you approve it. ') +
        'Trades you do not approve expire before the next open.',
      confirmLabel: 'Approve each trade',
      ticket: {
        live: book.live,
        lines: [
          { label: 'Strategy', value: name },
          { label: 'Portfolio', value: book.name },
          { label: 'Follow', value: modeLabel('approve') },
        ],
      },
    });
  }

  private async change(body: SubscriptionUpdate, done: string): Promise<void> {
    this.busy.set(true);
    try {
      const updated = await this.api.update(this.current().id, body);
      this.current.set(updated);
      this.changed.emit(updated);
      this.toasts.success(done);
    } catch {
      // The error interceptor already showed the API's message (a cancelled
      // step-up included), so only the radio needs putting back.
      this.revert();
    } finally {
      this.busy.set(false);
    }
  }

  /** Re-check the stored mode's radio after a refusal (the browser moved it). */
  private revert(): void {
    const mode = this.current().mode;
    const radio = [
      ...this.host.nativeElement.querySelectorAll<HTMLInputElement>('input[type="radio"]'),
    ].find((r) => r.value === mode);
    if (radio) radio.checked = true;
  }
}
