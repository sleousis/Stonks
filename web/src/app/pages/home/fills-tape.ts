import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { FillView, OrderView } from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import {
  formatDate,
  formatMoney,
  formatNumber,
  formatTime,
  formatWeekday,
} from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { autoRefresh } from '../../shared/auto-refresh';
import { SideTag } from '../../shared/ui/side-tag';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { HomeStore } from './home-store';

const DAY_MS = 24 * 60 * 60 * 1000;
const FILLS_LIMIT = 50;
/** A phone lists this many fills; the rest are one tap away. */
export const PHONE_FILLS = 4;

/**
 * The fills of the latest trading day that had any (within a week), oldest
 * first, like a tape reads. A daily run fills at its session's date, so on
 * Monday morning the tape still shows Friday's fills. `today` is true when
 * that day is today (then each fill shows its time).
 */
export function latestFills(
  fills: readonly FillView[],
  now = new Date(),
): { day: string | null; today: boolean; fills: FillView[] } {
  const since = now.getTime() - 7 * DAY_MS;
  const recent = fills.filter((f) => Date.parse(f.filled_at) >= since);
  if (!recent.length) return { day: null, today: false, fills: [] };
  const latest = recent.reduce((a, b) =>
    Date.parse(b.filled_at) > Date.parse(a.filled_at) ? b : a,
  );
  const day = formatDate(latest.filled_at);
  return {
    day: latest.filled_at,
    today: day === formatDate(now.toISOString()),
    fills: recent
      .filter((f) => formatDate(f.filled_at) === day)
      .sort((a, b) => Date.parse(a.filled_at) - Date.parse(b.filled_at)),
  };
}

/** A fill's side comes from its order (fills carry the order's client id). */
export function sideByOrder(orders: readonly OrderView[]): Map<string, string> {
  return new Map(orders.map((o) => [o.client_id, o.side]));
}

interface TapeItem {
  id: number;
  side: string | null;
  ticker: string;
  quantity: string;
  price: string;
  time: string | null;
}

/**
 * The fills tape: one row between hairlines under the overview. "Fills" and
 * a count badge, then each fill of the latest session (side tag, ticker,
 * quantity at price, time) with thin rules between, fading out on the
 * right. The row scrolls sideways inside itself, never the page. On a phone
 * it becomes a short list of the first fills.
 */
@Component({
  selector: 'app-fills-tape',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, SideTag],
  template: `
    @if (show()) {
      <section class="tape" aria-labelledby="home-fills">
        <h2 id="home-fills" class="label">
          Fills
          @if (items().length) {
            <span class="count-badge">{{ items().length }}</span>
          }
          @if (dayLabel(); as d) {
            <span class="day">{{ d }}</span>
          }
        </h2>
        <div class="window" [class.scrolls]="items().length > 0">
          @if (fills.error()) {
            <p class="note">
              Could not load fills.
              <button type="button" class="btn btn-link" (click)="fills.reload()">Try again</button>
            </p>
          } @else if (!fills.hasValue() || !store.orders.hasValue()) {
            <p class="note">Loading fills</p>
          } @else if (items().length === 0) {
            <p class="note">No fills this week. They show here as trading runs fill orders.</p>
          } @else {
            <ul class="run" aria-label="Latest fills">
              @for (f of items(); track f.id; let i = $index) {
                <li [class.extra]="i >= phoneCount">
                  <app-side-tag [side]="f.side" />
                  <span class="ticker">{{ f.ticker }}</span>
                  <span class="num">{{ f.quantity }}</span>
                  <span class="at">at</span>
                  <span class="num">{{ f.price }}</span>
                  @if (f.time) {
                    <span class="num time">{{ f.time }}</span>
                  }
                </li>
              }
            </ul>
          }
        </div>
        <a class="all text-link" routerLink="/orders/fills">All fills</a>
      </section>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .tape {
      display: flex;
      align-items: center;
      gap: var(--space-4);
      min-height: 52px;
      border-block: 1px solid var(--rule);
      font-size: var(--text-md);
    }
    .label {
      display: inline-flex;
      flex: none;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
    }
    .day {
      color: var(--muted);
      font-weight: var(--weight-regular);
    }
    .window {
      flex: 1;
      min-width: 0;
      overflow-x: auto;
      scrollbar-width: none;
    }
    .window::-webkit-scrollbar {
      display: none;
    }
    .window.scrolls {
      mask-image: linear-gradient(90deg, black calc(100% - 48px), transparent);
    }
    .note {
      margin: 0;
      color: var(--ink-2);
      font-size: var(--text-sm);
    }
    .run {
      display: flex;
      width: max-content;
      margin: 0;
      padding: 0 48px 0 0;
      list-style: none;
    }
    li {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      padding: 0 var(--space-5);
      border-left: 1px solid var(--rule);
      white-space: nowrap;
    }
    li:first-child {
      padding-left: 0;
      border-left: 0;
    }
    .ticker {
      font-weight: var(--weight-bold);
    }
    .at,
    .time {
      color: var(--muted);
    }
    .all {
      flex: none;
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      padding-left: var(--space-4);
      border-left: 1px solid var(--rule);
    }
    /* Phones: a short stacked list, "All fills" beside the title. */
    @include bp.phone {
      .tape {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 0 var(--space-3);
      }
      .label {
        min-height: var(--touch-min);
      }
      .all {
        grid-column: 2;
        grid-row: 1;
        padding-left: 0;
        border-left: 0;
      }
      .window {
        grid-column: 1 / -1;
        overflow: visible;
      }
      .window.scrolls {
        mask-image: none;
      }
      .note {
        padding-bottom: var(--space-3);
      }
      .run {
        display: grid;
        width: auto;
        padding: 0;
      }
      li {
        min-height: var(--touch-min);
        padding: 0;
        border-left: 0;
        border-top: 1px solid var(--rule);
      }
      li:first-child {
        border-left: 0;
      }
      li.extra {
        display: none;
      }
      .time {
        margin-left: auto;
      }
    }
  `,
})
export class FillsTape {
  private readonly api = inject(OrdersService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly watch = inject(WatchlistContextService);
  protected readonly store = inject(HomeStore);
  protected readonly phoneCount = PHONE_FILLS;

  constructor() {
    this.store.need('orders');
  }

  /** Nothing to show (and nothing asked) for someone without a portfolio. */
  protected readonly show = computed(() => this.store.hasPortfolios());

  protected readonly fills = resource({
    params: () => ({ portfolio: this.ctx.selectedId() }),
    loader: async (): Promise<FillView[]> => {
      await this.ctx.load();
      if (!this.show()) return [];
      return (await this.api.fills({ limit: FILLS_LIMIT })).items;
    },
  });
  /** New fills show during the session, and right after a trading run starts (UX-12). */
  protected readonly auto = autoRefresh(() => [this.fills], {
    triggers: [inject(TradingDayService).runsPassed],
  });

  private readonly latest = computed(() =>
    this.fills.hasValue()
      ? latestFills(this.fills.value())
      : { day: null, today: false, fills: [] as FillView[] },
  );
  /** The weekday when the tape shows an earlier session's fills. */
  protected readonly dayLabel = computed(() => {
    const { day, today } = this.latest();
    return day && !today ? formatWeekday(day) : null;
  });
  protected readonly items = computed<TapeItem[]>(() => {
    if (!this.fills.hasValue()) return [];
    const sides = sideByOrder(this.store.orders.hasValue() ? this.store.orders.value() : []);
    const { fills, today } = this.latest();
    return fills
      .filter((f) => this.watch.keeps(f.ticker))
      .map((f) => ({
        id: f.id,
        side: sides.get(f.order_client_id) ?? null,
        ticker: f.ticker,
        quantity: formatNumber(Math.abs(f.quantity)),
        price: formatMoney(f.price),
        time: today ? formatTime(f.filled_at) : null,
      }));
  });
}
