import { Injectable, computed, inject, linkedSignal, resource, signal } from '@angular/core';

import type { OrderView, PnlSeries, PortfolioView } from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { PortfolioService } from '../../api/portfolio.service';
import { ShadowService } from '../../api/shadow.service';
import { type SubscriptionView, SubscriptionsService } from '../../api/subscriptions.service';
import { isoDay } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';

const ORDERS_LIMIT = 50;
/** The strategy rows draw the last 30 days of paper returns. */
const SPARK_DAYS = 30;
const DAY_MS = 24 * 60 * 60 * 1000;

/** Orders that still wait for a fill. */
export const OPEN_ORDER_STATUSES: ReadonlySet<string> = new Set([
  'pending',
  'submitted',
  'partially_filled',
]);

/** The reads a section can ask the store for. */
export type HomeRead = 'portfolio' | 'orders' | 'follows' | 'paper';

/** One followed strategy's paper record: its return and its last 30 days. */
export interface PaperRecord {
  cumulativeReturn: number | null;
  points: number[];
}

/**
 * Today's shared reads. Several sections show the same data (the overview
 * and the holdings read the portfolio, the strategy rows and the paper-days
 * meter read the follows, the fills tape and the open orders figure read
 * the orders), so each is asked for once here. Provided by the Today page.
 * A read starts once a section asks for it with `need()`, so a section
 * shown on its own only asks for what it shows.
 */
@Injectable()
export class HomeStore {
  private readonly portfolioApi = inject(PortfolioService);
  private readonly ordersApi = inject(OrdersService);
  private readonly subsApi = inject(SubscriptionsService);
  private readonly shadowApi = inject(ShadowService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly wanted = signal<ReadonlySet<HomeRead>>(new Set());

  /** Start these reads (each once). Call from a section's constructor. */
  need(...reads: HomeRead[]): void {
    this.wanted.update((w) => new Set([...w, ...reads]));
  }
  private wants(read: HomeRead): boolean {
    return this.wanted().has(read);
  }

  /**
   * Null when the person has no portfolio (they follow strategies for
   * signals only): then nothing asks for holdings, so no 404 (BUG-3).
   */
  readonly portfolio = resource({
    params: () => (this.wants('portfolio') ? { portfolio: this.ctx.selectedId() } : undefined),
    loader: () => this.ifBook<PortfolioView>(() => this.portfolioApi.get()),
  });
  readonly pnl = resource({
    params: () => (this.wants('portfolio') ? { portfolio: this.ctx.selectedId() } : undefined),
    loader: () => this.ifBook<PnlSeries>(() => this.portfolioApi.pnl()),
  });
  /** The latest orders: the tape reads their sides, the overview counts the open ones. */
  readonly orders = resource({
    params: () => (this.wants('orders') ? { portfolio: this.ctx.selectedId() } : undefined),
    loader: async (): Promise<OrderView[]> => {
      await this.ctx.load();
      if (!this.hasPortfolios()) return [];
      return (await this.ordersApi.list({ limit: ORDERS_LIMIT })).items;
    },
  });
  /** Every strategy the person follows. */
  readonly subs = resource({
    params: () => (this.wants('follows') ? {} : undefined),
    loader: () => this.subsApi.list(),
  });

  /** The follows as shown, updated in place from each change's response. */
  readonly follows = linkedSignal<SubscriptionView[]>(() =>
    this.subs.hasValue() ? this.subs.value() : [],
  );

  /** The followed strategy ids, changing only when the set does (not on a mode switch). */
  private readonly followedIds = computed(
    () => [...new Set(this.follows().map((s) => s.strategy_id))].sort(),
    { equal: (a, b) => a.join() === b.join() },
  );

  /**
   * Paper records of the followed strategies, from their model books. Silent:
   * a strategy without one just shows no return and no line.
   */
  readonly paper = resource({
    params: () => {
      if (!this.wants('paper')) return undefined;
      const ids = this.followedIds();
      return ids.length ? { ids } : undefined;
    },
    loader: async ({ params }): Promise<Map<string, PaperRecord>> => {
      const out = new Map<string, PaperRecord>();
      try {
        const summaries = (await this.shadowApi.pnlSummaries()).items;
        const wanted = summaries.filter((s) => params.ids.includes(s.strategy_id));
        const since = isoDay(new Date(Date.now() - SPARK_DAYS * DAY_MS));
        await Promise.all(
          wanted.map(async (s) => {
            let points: number[] = [];
            try {
              const series = await this.shadowApi.pnl(s.strategy_id, { since });
              points = series.rows.map((r) => r.total_value);
            } catch {
              // The return still shows without its line.
            }
            out.set(s.strategy_id, { cumulativeReturn: s.cumulative_return, points });
          }),
        );
      } catch {
        // No paper records: the rows show without them.
      }
      return out;
    },
  });

  /** True once the person is known to have at least one portfolio (or still loading). */
  readonly hasPortfolios = computed(
    () => !(this.ctx.state() === 'ready' && this.ctx.options().length === 0),
  );

  readonly openOrders = computed(() =>
    this.orders.hasValue()
      ? this.orders.value().filter((o) => OPEN_ORDER_STATUSES.has(o.status)).length
      : null,
  );

  /** Keep the list in step with a change the follow control made. */
  replaceFollow(updated: SubscriptionView): void {
    this.follows.update((list) => list.map((s) => (s.id === updated.id ? updated : s)));
  }

  private async ifBook<T>(read: () => Promise<T>): Promise<T | null> {
    await this.ctx.load();
    if (this.ctx.noBook()) return null;
    return read();
  }
}
