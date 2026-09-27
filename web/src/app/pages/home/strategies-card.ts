import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

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
import { modeLabel } from '../../shared/governance-labels';
import { strategyDisplayName } from '../../shared/strategy-names';
import { HelpTip } from '../../shared/ui/help-tip';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { MODES, autoBlockedReason } from './strategy-modes';

interface Row {
  sub: SubscriptionView;
  name: string;
  modeLabel: string;
  autoReason: string | null;
  help: string;
}

/**
 * My strategies: an on/off switch and a mode switch (Signals only, Paper
 * trading, Auto) for each strategy the trader follows. Auto stays disabled
 * with its reason until the auto gate passes. Turning auto on, or switching
 * an auto strategy back on, asks for a fresh code and an order ticket.
 */
@Component({
  selector: 'app-strategies-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill, HelpTip, LoadingState, EmptyState, ErrorState, PermissionNote],
  template: `
    <section class="panel" aria-labelledby="home-strategies">
      <div class="panel-head">
        <h2 id="home-strategies">My strategies</h2>
        <a routerLink="/strategies" class="more">Follow a strategy</a>
      </div>
      @if (subs.error(); as err) {
        <app-error-state
          title="Could not load your strategies"
          [error]="err"
          (retry)="subs.reload()"
        />
      } @else if (!subs.hasValue()) {
        <app-loading-state label="Loading your strategies" [rows]="3" />
      } @else if (rows().length === 0) {
        <app-empty-state
          title="You follow no strategies yet"
          message="Open a strategy and press Follow to get its signals here, or paper trade it."
        >
          <a routerLink="/strategies" class="btn btn-primary">Follow a strategy</a>
        </app-empty-state>
      } @else {
        <ul class="list">
          @for (row of rows(); track row.sub.id) {
            <li>
              <div class="head">
                <div class="name">
                  <a [routerLink]="['/strategies', row.sub.strategy_id]">{{ row.name }}</a>
                  @if (row.sub.strategy_status !== 'active') {
                    <app-status-pill [status]="row.sub.strategy_status" />
                  }
                </div>
                <button
                  type="button"
                  role="switch"
                  class="switch"
                  [attr.aria-checked]="row.sub.enabled"
                  [attr.aria-label]="'Follow ' + row.name"
                  [disabled]="busy().has(row.sub.id) || !canTrade()"
                  (click)="toggle(row.sub)"
                >
                  <span class="track" aria-hidden="true"><span class="thumb"></span></span>
                  <span class="state">{{ row.sub.enabled ? 'On' : 'Off' }}</span>
                </button>
              </div>

              <fieldset class="modes" [disabled]="!row.sub.enabled || !canTrade()">
                <legend class="visually-hidden">Mode for {{ row.name }}</legend>
                @for (m of modes; track m.value) {
                  <label
                    class="mode"
                    [class.on]="row.sub.mode === m.value"
                    [class.locked]="m.value === 'auto' && row.autoReason && row.sub.mode !== 'auto'"
                  >
                    <input
                      type="radio"
                      [name]="'mode-' + row.sub.id"
                      [value]="m.value"
                      [checked]="row.sub.mode === m.value"
                      [disabled]="
                        busy().has(row.sub.id) ||
                        (m.value === 'auto' && !canAuto()) ||
                        (m.value === 'auto' && !!row.autoReason && row.sub.mode !== 'auto')
                      "
                      [attr.aria-describedby]="
                        m.value === 'auto' && row.autoReason ? 'auto-why-' + row.sub.id : null
                      "
                      (change)="setMode(row.sub, m.value)"
                    />
                    {{ m.label }}
                  </label>
                }
              </fieldset>

              @if (row.sub.paused_reason) {
                <p class="note warn">Auto is paused. {{ row.sub.paused_reason }}</p>
              }
              <p class="help">{{ row.help }} <app-help-tip [term]="row.modeLabel" /></p>
              @if (row.autoReason && row.sub.mode !== 'auto') {
                <p class="why" [id]="'auto-why-' + row.sub.id">{{ row.autoReason }}</p>
              }
            </li>
          }
        </ul>
        <div class="gate">
          <app-permission-note permission="portfolio.manage" />
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .more {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      font-size: var(--text-sm);
    }
    .list {
      display: grid;
      margin: 0;
      padding: 0 var(--space-4);
      list-style: none;
    }
    li {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--color-border);
    }
    li:last-child {
      border-bottom: 0;
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
    .name a {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
    }
    .switch {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--touch-min);
      padding: 0 var(--space-1);
      border: 0;
      background: none;
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
    .modes {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin: 0;
      padding: 0;
      border: 1px solid var(--color-border-strong);
      border-radius: var(--radius-sm);
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
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
      cursor: pointer;
      border-left: 1px solid var(--color-border-strong);
    }
    .mode:first-of-type {
      border-left: 0;
    }
    .mode input {
      position: absolute;
      opacity: 0;
      inset: 0;
      margin: 0;
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
    .gate {
      padding: 0 var(--space-4);
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
  `,
})
export class StrategiesCard {
  private readonly api = inject(SubscriptionsService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  protected readonly modes = MODES;
  /** Switching strategies on or off and changing modes. */
  protected readonly canTrade = computed(() => this.session.can('portfolio.manage'));
  /** Auto places real orders: its own permission. */
  protected readonly canAuto = computed(() => this.session.can('subscription.auto_enable'));
  private readonly portfolios = inject(PortfolioContextService);
  /** Subscriptions with a change in flight, so one finishing never frees another (UX-59). */
  protected readonly busy = signal<ReadonlySet<string>>(new Set());

  protected readonly subs = resource({ loader: () => this.api.list() });

  /** The list as shown, updated in place from each change's response. */
  private readonly items = linkedSignal(() => (this.subs.hasValue() ? this.subs.value() : []));
  protected readonly rows = computed<Row[]>(() =>
    this.items().map((sub) => ({
      sub,
      name: strategyDisplayName(sub.strategy_id),
      modeLabel: modeLabel(sub.mode),
      autoReason: autoBlockedReason(sub),
      help: MODES.find((m) => m.value === sub.mode)?.help ?? '',
    })),
  );

  protected async toggle(sub: SubscriptionView): Promise<void> {
    if (this.busy().has(sub.id)) return;
    const on = !sub.enabled;
    const name = strategyDisplayName(sub.strategy_id);
    // Switching an auto strategy back on restarts real orders (UX-02).
    if (on && sub.mode === 'auto' && !(await this.confirmAuto(sub, true))) return;
    await this.change(sub, { enabled: on }, `${name} is ${on ? 'on' : 'off'}.`);
  }

  protected async setMode(sub: SubscriptionView, mode: SubscriptionMode): Promise<void> {
    if (mode === sub.mode) return;
    if (mode === 'auto' && !(await this.confirmAuto(sub, false))) {
      this.revert(sub);
      return;
    }
    const name = strategyDisplayName(sub.strategy_id);
    await this.change(sub, { mode }, `${name} is now on ${modeLabel(mode).toLowerCase()}.`);
  }

  /**
   * A fresh code, then an order ticket with the name typed (UX-02, UX-14).
   * Brass unless the portfolio is known to trade paper money.
   */
  private async confirmAuto(sub: SubscriptionView, again: boolean): Promise<boolean> {
    const name = strategyDisplayName(sub.strategy_id);
    const verb = again ? 'Turn auto back on' : 'Turn on auto';
    if (!(await this.stepUp.ensure(`${verb} for ${name}.`))) return false;
    const book = this.portfolios.options().find((p) => p.id === sub.portfolio_id) ?? null;
    return this.confirm.confirm({
      title: `${verb} for ${name}?`,
      message:
        'Stonks will place orders with your broker for this strategy on every trading run, ' +
        'without asking each time. You can switch back to paper trading at any time.',
      confirmLabel: verb,
      tone: 'danger',
      typedConfirmation: sub.strategy_id,
      ticket: {
        live: book ? book.trading === 'live' : true,
        lines: [
          { label: 'Strategy', value: name },
          { label: 'Portfolio', value: book?.name ?? 'Your portfolio' },
          { label: 'Mode', value: modeLabel('auto') },
        ],
      },
    });
  }

  private async change(sub: SubscriptionView, body: SubscriptionUpdate, done: string) {
    this.busy.update((ids) => new Set(ids).add(sub.id));
    try {
      const updated = await this.api.update(sub.id, body);
      this.items.update((list) => list.map((s) => (s.id === updated.id ? updated : s)));
      this.toasts.success(done);
    } catch {
      // The error interceptor already showed the API's message (a cancelled
      // step-up included), so only the radio needs putting back.
      this.revert(sub);
    } finally {
      this.busy.update((ids) => {
        const next = new Set(ids);
        next.delete(sub.id);
        return next;
      });
    }
  }

  /** Re-check the stored mode's radio after a refusal (the browser moved it). */
  private revert(sub: SubscriptionView): void {
    const current = this.items().find((s) => s.id === sub.id)?.mode ?? sub.mode;
    const radio = [
      ...this.host.nativeElement.querySelectorAll<HTMLInputElement>('input[type="radio"]'),
    ].find((r) => r.name === `mode-${sub.id}` && r.value === current);
    if (radio) radio.checked = true;
  }
}
