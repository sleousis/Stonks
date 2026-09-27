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
import { BrandMark } from '../../shared/ui/brand-mark';
import { SideTag } from '../../shared/ui/side-tag';
import { TradingDayService } from '../../core/schedule/trading-day.service';

const DAY_MS = 24 * 60 * 60 * 1000;
const FILLS_LIMIT = 50;
/** The tape moves once it holds this many fills; fewer sit still. */
export const TAPE_MOVES_FROM = 4;

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
 * A thin tape of the latest session's fills: side, ticker, quantity at
 * price, and the time (or the weekday on the label for an earlier session). It scrolls slowly when there are enough fills to fill it, stops
 * while hovered or focused, and stays still under reduced motion.
 */
@Component({
  selector: 'app-fills-tape',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, SideTag, BrandMark],
  template: `
    @if (show()) {
      <section class="tape" aria-label="Latest fills">
        <span class="label"
          >Fills
          @if (dayLabel(); as d) {
            <span class="day">{{ d }}</span>
          }
        </span>
        <div class="window">
          @if (fills.error()) {
            <p class="note">
              Could not load fills.
              <button type="button" class="link" (click)="fills.reload()">Try again</button>
            </p>
          } @else if (!fills.hasValue()) {
            <p class="note">
              <app-brand-mark mode="loading" [size]="16" /> <span>Loading fills</span>
            </p>
          } @else if (items().length === 0) {
            <p class="note">No fills this week. They show here as trading runs fill orders.</p>
          } @else {
            <div class="track" [class.moving]="moving()">
              <ul class="run">
                @for (f of items(); track f.id) {
                  <li>
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
              @if (moving()) {
                <ul class="run" aria-hidden="true">
                  @for (f of items(); track f.id) {
                    <li>
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
          }
        </div>
        <a class="all" routerLink="/orders/fills">All fills</a>
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
      align-items: stretch;
      min-height: var(--touch-min);
      border-block: 1px solid var(--color-ink);
      background: var(--color-surface);
      font-size: var(--text-sm);
    }
    .label {
      display: flex;
      align-items: center;
      padding: 0 var(--space-3);
      background: var(--color-ink);
      color: var(--color-surface);
      font-family: var(--font-display);
      font-stretch: var(--display-stretch);
      font-weight: var(--weight-bold);
      letter-spacing: 0.02em;
    }
    .day {
      margin-left: var(--space-1);
      font-weight: var(--weight-regular);
      opacity: 0.8;
    }
    .window {
      position: relative;
      flex: 1;
      min-width: 0;
      overflow: hidden;
      display: flex;
      align-items: center;
      mask-image: linear-gradient(
        90deg,
        transparent,
        black var(--space-4),
        black calc(100% - var(--space-5)),
        transparent
      );
    }
    .note {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      margin: 0;
      padding: 0 var(--space-3);
      color: var(--color-ink-2);
    }
    .link {
      min-height: var(--touch-min);
      padding: 0;
      border: 0;
      background: none;
      color: var(--color-primary);
      text-decoration: underline;
      cursor: pointer;
    }
    .track {
      display: flex;
      width: max-content;
    }
    .run {
      display: flex;
      gap: var(--space-5);
      margin: 0;
      padding: 0 var(--space-5) 0 var(--space-3);
      list-style: none;
    }
    li {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      white-space: nowrap;
    }
    .ticker {
      font-weight: var(--weight-semibold);
    }
    .at,
    .time {
      color: var(--color-ink-3);
    }
    .moving {
      animation: tape var(--tape-speed) linear infinite;
    }
    .tape:hover .moving,
    .tape:focus-within .moving {
      animation-play-state: paused;
    }
    @keyframes tape {
      to {
        transform: translateX(-50%);
      }
    }
    .all {
      display: flex;
      align-items: center;
      padding: 0 var(--space-3);
      border-left: 1px solid var(--color-border);
      white-space: nowrap;
    }
    /* Reduced motion: the tape sits still and scrolls by hand. */
    @media (prefers-reduced-motion: reduce) {
      .day {
        margin-left: var(--space-1);
        font-weight: var(--weight-regular);
        opacity: 0.8;
      }
      .window {
        overflow-x: auto;
        mask-image: none;
      }
      .moving {
        animation: none;
      }
      .run[aria-hidden='true'] {
        display: none;
      }
    }
    @include bp.phone {
      .label {
        padding: 0 var(--space-2);
      }
    }
  `,
})
export class FillsTape {
  private readonly api = inject(OrdersService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly watch = inject(WatchlistContextService);

  /** Nothing to show (and nothing asked) for someone without a portfolio. */
  protected readonly show = computed(
    () => !(this.ctx.state() === 'ready' && this.ctx.options().length === 0),
  );

  protected readonly fills = resource({
    params: () => ({ portfolio: this.ctx.selectedId() }),
    loader: async () => {
      await this.ctx.load();
      if (!this.show()) return { fills: [], sides: new Map<string, string>() };
      const [fills, orders] = await Promise.all([
        this.api.fills({ limit: FILLS_LIMIT }),
        this.api.list({ limit: FILLS_LIMIT }),
      ]);
      return { fills: fills.items, sides: sideByOrder(orders.items) };
    },
  });
  /** New fills show during the session, and right after a trading run starts (UX-12). */
  protected readonly auto = autoRefresh(() => [this.fills], {
    triggers: [inject(TradingDayService).runsPassed],
  });

  private readonly latest = computed(() =>
    this.fills.hasValue()
      ? latestFills(this.fills.value().fills)
      : { day: null, today: false, fills: [] as FillView[] },
  );
  /** The weekday when the tape shows an earlier session's fills. */
  protected readonly dayLabel = computed(() => {
    const { day, today } = this.latest();
    return day && !today ? formatWeekday(day) : null;
  });
  protected readonly items = computed<TapeItem[]>(() => {
    if (!this.fills.hasValue()) return [];
    const sides = this.fills.value().sides;
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
  protected readonly moving = computed(() => this.items().length >= TAPE_MOVES_FROM);
}
