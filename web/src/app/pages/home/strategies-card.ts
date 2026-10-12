import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { SubscriptionView } from '../../api/subscriptions.service';
import { formatPercent, toneClass } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { FollowControl } from '../../shared/follow/follow-control';
import { isGatedMode, lockedModesLine } from '../../shared/follow/follow-rules';
import { rowStrategyName, strategyDisplayName } from '../../shared/strategy-names';
import { HelpTip } from '../../shared/ui/help-tip';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { HomeStore } from './home-store';
import { Sparkline } from '../../shared/chart/sparkline';

interface Row {
  sub: SubscriptionView;
  name: string;
  /** The portfolio it trades, when the follows sit in more than one. */
  portfolio: string | null;
  /** The strategy's paper return so far, signed, or null without a record. */
  ret: string | null;
  tone: string;
  points: readonly number[];
}

/**
 * Strategies I follow: one row per strategy, its paper return and the last
 * 30 days as a small line, then `<app-follow-control>` (M8), the same
 * control as the strategy page: an on and off switch, the follow modes
 * (Alerts only, Paper, Approve each trade, Automatic), and why the gated
 * modes are closed. The unlock rule is said once under the list; each row
 * says only how far it has come ("Your paper days: 4 of 20."). Turning a
 * gated mode on, or switching an automatic follow back on, asks for a fresh
 * code and a ticket, and Automatic asks for the strategy's name as shown.
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
    Sparkline,
  ],
  template: `
    <section class="page-section" aria-labelledby="home-strategies">
      <header class="section-head">
        <h2 id="home-strategies">
          Strategies I follow
          @if (rows().length) {
            <span class="count">{{ rows().length }}</span>
          }
        </h2>
        <a routerLink="/strategies" class="text-link">Follow a strategy</a>
      </header>
      @if (store.subs.error(); as err) {
        <div class="section-body">
          <app-error-state
            title="Could not load your strategies"
            [error]="err"
            (retry)="store.subs.reload()"
          />
        </div>
      } @else if (!store.subs.hasValue()) {
        <div class="section-body">
          <app-loading-state label="Loading your strategies" [rows]="3" />
        </div>
      } @else if (rows().length === 0) {
        <div class="section-body">
          <app-empty-state
            title="You follow no strategies yet"
            message="Open a strategy and press Follow to get its signals here, or trade it on paper."
          >
            <a routerLink="/strategies" class="btn btn-primary">Follow a strategy</a>
          </app-empty-state>
        </div>
      } @else {
        <ul class="list">
          @for (row of rows(); track row.sub.id) {
            <li>
              <app-follow-control
                [sub]="row.sub"
                [strategyName]="row.name"
                [compact]="true"
                (changed)="store.replaceFollow($event)"
              >
                <span class="who">
                  <a class="strategy" [routerLink]="['/strategies', row.sub.strategy_id]">{{
                    row.name
                  }}</a>
                  @if (row.sub.strategy_status !== 'active' || row.portfolio) {
                    <span class="meta">
                      @if (row.sub.strategy_status !== 'active') {
                        <app-status-pill [status]="row.sub.strategy_status" />
                      }
                      @if (row.portfolio) {
                        <span class="where">In {{ row.portfolio }}</span>
                      }
                    </span>
                  }
                </span>
                @if (row.points.length > 1) {
                  <app-sparkline class="spark" [values]="row.points" />
                }
                @if (row.ret; as r) {
                  <span class="ret num" [class]="row.tone"
                    ><span class="visually-hidden">Paper return </span>{{ r }}</span
                  >
                }
              </app-follow-control>
            </li>
          }
        </ul>
        <div class="foot">
          @if (rule(); as r) {
            <p class="rule">{{ r }} <app-help-tip term="Paper" /></p>
          } @else {
            <p class="rule">Paper trades your paper portfolio. No real money moves.</p>
          }
          <app-permission-note permission="portfolio.manage" />
        </div>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .list {
      display: grid;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    /* One strategy per row, a hairline between. */
    li {
      padding: var(--space-4) 0;
      border-bottom: 1px solid var(--rule);
    }
    /* The head of each row: name and meta, the 30-day line, the return. */
    .who {
      display: grid;
      flex: 1 1 12rem;
      gap: 2px;
      min-width: 0;
    }
    .strategy {
      align-self: start;
      color: var(--ink);
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      text-decoration: none;
    }
    .strategy:hover,
    .strategy:focus-visible {
      text-decoration: underline;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .where {
      color: var(--muted);
      font-size: var(--text-sm);
      font-weight: var(--weight-regular);
    }
    .spark {
      flex: none;
    }
    .ret {
      flex: none;
      min-width: 5rem;
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
      text-align: right;
    }
    /* Desktop: one row per strategy. The name, its line and return, then the
       modes and the switch on the same line, the helper lines under the name.
       The follow control is shared, so its parts are placed from here. */
    @include bp.from-desktop {
      :host ::ng-deep app-follow-control {
        grid-template-columns: minmax(0, 1fr) 17rem auto;
        column-gap: var(--space-5);
        row-gap: var(--space-1);
        align-items: center;

        > .head {
          display: contents;
        }
        > .head > .name {
          grid-column: 1;
          grid-row: 1;
          flex-wrap: nowrap;
          gap: var(--space-5);
        }
        > .modes {
          grid-column: 2;
          grid-row: 1;
        }
        > .head > .switch {
          grid-column: 3;
          grid-row: 1;
        }
        > p {
          grid-column: 1;
          margin: 0;
        }
      }
    }
    .foot {
      display: grid;
      gap: var(--space-2);
      padding-top: var(--space-3);
    }
    .rule {
      max-width: 68ch;
      color: var(--ink-2);
      font-size: var(--text-sm);
    }
    /* Phones: the name and return on one line, the line under at full width. */
    @include bp.phone {
      .who {
        flex-basis: calc(100% - 6rem);
      }
      .spark {
        order: 3;
        width: 100%;
      }
      .ret {
        order: 2;
        min-width: 0;
        font-size: var(--text-lead);
      }
    }
  `,
})
export class StrategiesCard {
  protected readonly store = inject(HomeStore);
  private readonly portfolios = inject(PortfolioContextService);

  constructor() {
    this.store.need('follows', 'paper');
  }

  protected readonly rows = computed<Row[]>(() => {
    const books = this.portfolios.options();
    const follows = this.store.follows();
    const paper = this.store.paper.hasValue() ? this.store.paper.value() : null;
    const several = new Set(follows.map((s) => s.portfolio_id)).size > 1;
    return follows.map((sub) => {
      const record = paper?.get(sub.strategy_id) ?? null;
      const value = record?.cumulativeReturn ?? null;
      return {
        sub,
        name: rowStrategyName(sub) ?? strategyDisplayName(sub.strategy_id),
        portfolio: several ? (books.find((p) => p.id === sub.portfolio_id)?.name ?? null) : null,
        ret: value == null ? null : formatPercent(value, { signed: true, digits: 2 }),
        tone: toneClass(value),
        points: record?.points ?? [],
      };
    });
  });

  /** The unlock line once, for the follows whose gated modes are still closed (M8, FR-03). */
  protected readonly rule = computed(() => {
    for (const s of this.store.follows()) {
      const line = isGatedMode(s.mode) ? null : lockedModesLine(s);
      if (line) return line;
    }
    return null;
  });
}
