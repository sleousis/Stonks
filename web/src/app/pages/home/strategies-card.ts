import {
  ChangeDetectionStrategy,
  Component,
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
import { FollowMode } from '../../shared/ui/follow-mode';
import { MODES, autoBlockedReason, unlockRule } from './strategy-modes';

interface Row {
  sub: SubscriptionView;
  name: string;
  /** The portfolio it trades, when the follows sit in more than one. */
  portfolio: string | null;
  autoReason: string | null;
  /** The real-money modes the auto gate keeps closed for this row. */
  locked: readonly SubscriptionMode[];
  help: string;
}

/**
 * My strategies: an on/off switch and one compact mode control (Alerts
 * only, Paper, Approve each trade, Automatic) for each strategy the trader
 * follows (M8). The unlock rule is said once above the list; each row says
 * only how far it has come ("Paper days: 4 of 20"). The two real-money
 * modes stay locked until the auto gate passes. Turning one on, or
 * switching an automatic follow back on, asks for a fresh code and a ticket.
 */
@Component({
  selector: 'app-strategies-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    StatusPill,
    HelpTip,
    FollowMode,
    LoadingState,
    EmptyState,
    ErrorState,
    PermissionNote,
  ],
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
          message="Open a strategy and press Follow to get its signals here, or trade it on paper."
        >
          <a routerLink="/strategies" class="btn btn-primary">Follow a strategy</a>
        </app-empty-state>
      } @else {
        @if (rule(); as r) {
          <p class="rule">{{ r }} <app-help-tip term="Approve each trade" /></p>
        }
        <ul class="list">
          @for (row of rows(); track row.sub.id) {
            <li>
              <div class="head">
                <div class="name">
                  <a [routerLink]="['/strategies', row.sub.strategy_id]">{{ row.name }}</a>
                  @if (row.sub.strategy_status !== 'active') {
                    <app-status-pill [status]="row.sub.strategy_status" />
                  }
                  @if (row.portfolio) {
                    <span class="where">in {{ row.portfolio }}</span>
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

              <app-follow-mode
                class="mode"
                [value]="row.sub.mode"
                [label]="'Mode for ' + row.name"
                [locked]="row.locked"
                [disabled]="!row.sub.enabled || !canTrade() || busy().has(row.sub.id)"
                [describedBy]="'mode-help-' + row.sub.id"
                (changed)="setMode(row.sub, $event)"
              />
              <p class="help" [id]="'mode-help-' + row.sub.id">
                {{ row.help }}
                @if (row.autoReason && row.sub.mode !== 'auto') {
                  <span class="why">{{ row.autoReason }}</span>
                }
              </p>
              @if (row.sub.paused_reason) {
                <p class="note warn">Automatic is paused. {{ row.sub.paused_reason }}</p>
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
    .mode {
      max-width: 18rem;
    }
    .where {
      font-size: var(--text-xs);
      font-weight: var(--weight-regular);
      color: var(--color-ink-3);
    }
    .rule {
      margin: 0;
      padding: var(--space-3) var(--space-4) 0;
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .gate {
      padding: 0 var(--space-4);
    }
    .help {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .why {
      display: block;
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
  protected readonly rows = computed<Row[]>(() => {
    const books = this.portfolios.options();
    const several = new Set(this.items().map((s) => s.portfolio_id)).size > 1;
    return this.items().map((sub) => {
      const autoReason = autoBlockedReason(sub);
      const closed = !this.canAuto() || (!!autoReason && !this.isLive(sub.mode));
      return {
        sub,
        name: strategyDisplayName(sub.strategy_id),
        portfolio: several ? (books.find((p) => p.id === sub.portfolio_id)?.name ?? null) : null,
        autoReason,
        locked: closed ? (['approve', 'auto'] as const) : [],
        help: MODES.find((m) => m.value === sub.mode)?.help ?? '',
      };
    });
  });

  /** The unlock rule once, while any follow still waits on its paper days (M8). */
  protected readonly rule = computed(() => {
    const waiting = this.items().filter(
      (s) => s.paper_days_completed < s.paper_days_required && !this.isLive(s.mode),
    );
    return waiting.length ? unlockRule(waiting[0].paper_days_required) : null;
  });

  protected async toggle(sub: SubscriptionView): Promise<void> {
    if (this.busy().has(sub.id)) return;
    const on = !sub.enabled;
    const name = strategyDisplayName(sub.strategy_id);
    // Switching an auto strategy back on restarts real orders (UX-02).
    if (on && sub.mode === 'auto' && !(await this.confirmAuto(sub, true))) return;
    await this.change(sub, { enabled: on }, `${name} is ${on ? 'on' : 'off'}.`);
  }

  /** Approve each trade and Automatic can send real orders: both pass the auto gate. */
  protected isLive(mode: string): boolean {
    return mode === 'approve' || mode === 'auto';
  }

  protected async setMode(sub: SubscriptionView, mode: SubscriptionMode): Promise<void> {
    if (mode === sub.mode) return;
    const confirmed =
      mode === 'auto'
        ? await this.confirmAuto(sub, false)
        : mode === 'approve'
          ? await this.confirmApprove(sub)
          : true;
    if (!confirmed) return;
    const name = strategyDisplayName(sub.strategy_id);
    await this.change(sub, { mode }, `${name}: ${modeLabel(mode)}.`);
  }

  /**
   * A fresh code, then an order ticket with the name typed (UX-02, UX-14).
   * Brass unless the portfolio is known to trade paper money.
   */
  private async confirmAuto(sub: SubscriptionView, again: boolean): Promise<boolean> {
    const name = strategyDisplayName(sub.strategy_id);
    const verb = again ? 'Turn Automatic back on' : 'Turn on Automatic';
    if (!(await this.stepUp.ensure(`${verb} for ${name}.`))) return false;
    const book = this.portfolios.options().find((p) => p.id === sub.portfolio_id) ?? null;
    return this.confirm.confirm({
      title: `${verb} for ${name}?`,
      message:
        'Stonks will place orders with your broker for this strategy on every trading run, ' +
        'without asking each time. You can switch back to Paper at any time.',
      confirmLabel: verb,
      tone: book && book.trading !== 'live' ? 'default' : 'danger',
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

  /**
   * A fresh code, then an order ticket (no typed name: every order still
   * waits for the trader's own approval). Brass unless the portfolio is
   * known to trade paper money.
   */
  private async confirmApprove(sub: SubscriptionView): Promise<boolean> {
    const name = strategyDisplayName(sub.strategy_id);
    if (!(await this.stepUp.ensure(`Approve each trade for ${name}.`))) return false;
    const book = this.portfolios.options().find((p) => p.id === sub.portfolio_id) ?? null;
    return this.confirm.confirm({
      title: `Approve each trade for ${name}?`,
      message:
        'After each trading run, the orders this strategy wants wait for you under Approvals. ' +
        'Nothing goes to your broker until you approve it with a code. Unapproved orders expire ' +
        'before the next open.',
      confirmLabel: 'Approve each trade',
      ticket: {
        live: book ? book.trading === 'live' : true,
        lines: [
          { label: 'Strategy', value: name },
          { label: 'Portfolio', value: book?.name ?? 'Your portfolio' },
          { label: 'Mode', value: modeLabel('approve') },
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
      // step-up included). The mode control still shows the stored mode.
    } finally {
      this.busy.update((ids) => {
        const next = new Set(ids);
        next.delete(sub.id);
        return next;
      });
    }
  }
}
