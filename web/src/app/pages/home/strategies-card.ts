import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { type SubscriptionView, SubscriptionsService } from '../../api/subscriptions.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { FollowControl } from '../../shared/follow/follow-control';
import { isGatedMode, lockedModesLine } from '../../shared/follow/follow-rules';
import { rowStrategyName, strategyDisplayName } from '../../shared/strategy-names';
import { HelpTip } from '../../shared/ui/help-tip';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

interface Row {
  sub: SubscriptionView;
  name: string;
  /** The portfolio it trades, when the follows sit in more than one. */
  portfolio: string | null;
}

/**
 * My strategies: one `<app-follow-control>` per strategy the trader
 * follows (M8), the same control as the strategy page: an on/off switch,
 * the four follow modes (Alerts only, Paper, Approve each trade,
 * Automatic), and why the gated modes are closed. The unlock rule is said
 * once above the list; each row says only how far it has come ("Paper
 * days: 4 of 20."). Turning a gated mode on, or switching an automatic
 * follow back on, asks for a fresh code and a ticket, and Automatic asks
 * for the strategy's name as shown, never its id.
 */
@Component({
  selector: 'app-strategies-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    StatusPill,
    HelpTip,
    FollowControl,
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
          <p class="rule">{{ r }} <app-help-tip term="Paper" /></p>
        }
        <ul class="list">
          @for (row of rows(); track row.sub.id) {
            <li>
              <app-follow-control
                [sub]="row.sub"
                [strategyName]="row.name"
                [compact]="true"
                (changed)="replace($event)"
              >
                <a class="name table-link" [routerLink]="['/strategies', row.sub.strategy_id]">{{
                  row.name
                }}</a>
                @if (row.sub.strategy_status !== 'active') {
                  <app-status-pill [status]="row.sub.strategy_status" />
                }
                @if (row.portfolio) {
                  <span class="where">in {{ row.portfolio }}</span>
                }
              </app-follow-control>
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
    /* One strategy per row, a hairline between, on a 16px rhythm. */
    li {
      padding: var(--space-4) 0;
      border-bottom: 1px solid var(--color-border);
    }
    li:last-child {
      border-bottom: 0;
    }
    .name {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      font-weight: var(--weight-semibold);
    }
    .where {
      font-size: var(--text-xs);
      font-weight: var(--weight-regular);
      color: var(--color-ink-3);
    }
    .rule {
      margin: 0;
      max-width: 70ch;
      padding: var(--space-4) var(--space-4) 0;
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    @media (max-width: 767.98px) {
      .list,
      .gate {
        padding-inline: var(--space-3);
      }
      .rule {
        padding: var(--space-3) var(--space-3) 0;
      }
      li {
        padding: var(--space-3) 0;
      }
    }
    .gate {
      padding: 0 var(--space-4);
    }
  `,
})
export class StrategiesCard {
  private readonly api = inject(SubscriptionsService);
  private readonly portfolios = inject(PortfolioContextService);

  protected readonly subs = resource({ loader: () => this.api.list() });

  /** The list as shown, updated in place from each change's response. */
  private readonly items = linkedSignal(() => (this.subs.hasValue() ? this.subs.value() : []));
  protected readonly rows = computed<Row[]>(() => {
    const books = this.portfolios.options();
    const several = new Set(this.items().map((s) => s.portfolio_id)).size > 1;
    return this.items().map((sub) => ({
      sub,
      name: rowStrategyName(sub) ?? strategyDisplayName(sub.strategy_id),
      portfolio: several ? (books.find((p) => p.id === sub.portfolio_id)?.name ?? null) : null,
    }));
  });

  /** The unlock line once, for the follows whose gated modes are still closed (M8, FR-03). */
  protected readonly rule = computed(() => {
    for (const s of this.items()) {
      const line = isGatedMode(s.mode) ? null : lockedModesLine(s);
      if (line) return line;
    }
    return null;
  });

  /** Keep the list in step with a change the follow control made. */
  protected replace(updated: SubscriptionView): void {
    this.items.update((list) => list.map((s) => (s.id === updated.id ? updated : s)));
  }
}
