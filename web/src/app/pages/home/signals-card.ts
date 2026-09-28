import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { FeedItemView, TickRun } from '../../api/models';
import { NotificationsService } from '../../api/notifications.service';
import { TicksService } from '../../api/ticks.service';
import { formatTime, formatWeekday, isoDay } from '../../core/format/format';
import { NotificationFeedService, appLink } from '../../core/notify/notification-feed.service';
import { jobLabel, nextTradingRun } from '../../core/schedule/job-labels';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import {
  WatchlistContextService,
  signalTicker,
} from '../../core/watchlists/watchlist-context.service';
import { autoRefresh } from '../../shared/auto-refresh';
import { countdown } from '../../shared/ui/session-strip';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { RUN_WORDS } from '../../shared/status-words';

const DAY_MS = 24 * 60 * 60 * 1000;
const FEED_LIMIT = 100;
const TICKS_LIMIT = 10;

/**
 * Signals from the last 24 hours, newest first. Ticks run after the close,
 * so "today" is a rolling day: last night's signals still count this morning.
 */
export function todaysSignals(items: readonly FeedItemView[], now = new Date()): FeedItemView[] {
  const since = now.getTime() - DAY_MS;
  return items.filter((i) => i.category === 'signal' && new Date(i.created_at).getTime() >= since);
}

/** Trading runs started in the last 24 hours. */
export function todaysRuns(runs: readonly TickRun[], now = new Date()): TickRun[] {
  const since = now.getTime() - DAY_MS;
  return runs.filter((r) => new Date(r.started_at).getTime() >= since);
}

/**
 * The orders and fills a trading run made in the reader's own portfolios
 * (M15), or null when none of them took part. The API keeps a run's global
 * totals for everyone, and only the reader's books under `portfolio_id`
 * (a one-book run the reader owns) or `portfolios` (their books of a
 * many-book run), so the totals are never shown as the reader's.
 */
export function ownRunCounts(run: TickRun): { orders: number; fills: number } | null {
  const s = run.summary as (TickRun['summary'] & { portfolios?: unknown }) | null;
  if (!s) return null;
  if (s.portfolio_id) return { orders: s.orders_placed ?? 0, fills: s.fills ?? 0 };
  const books = s.portfolios;
  if (!books || typeof books !== 'object') return null;
  const mine = Object.values(books as Record<string, Record<string, unknown>>);
  if (!mine.length) return null;
  const sum = (key: string) =>
    mine.reduce((n, b) => n + (typeof b?.[key] === 'number' ? (b[key] as number) : 0), 0);
  return { orders: sum('orders_placed'), fills: sum('fills') };
}

/** One line of a trading run: "3 orders, 3 fills" in your portfolios, or its error. */
export function runDetail(run: TickRun): string {
  const s = run.summary;
  if (run.status === 'running') return 'Deciding and placing orders now.';
  if (s?.error) return s.error;
  const own = ownRunCounts(run);
  if (!own) return 'None of your portfolios traded in this run.';
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;
  return `${plural(own.orders, 'order')}, ${plural(own.fills, 'fill')} in your portfolios.`;
}

const RUN_TITLE: Record<TickRun['status'], string> = {
  running: 'Trading run going',
  ok: 'Trading run finished',
  partial: 'Trading run finished with problems',
  error: 'Trading run failed',
};

interface BlotterRow {
  key: string;
  kind: 'signal' | 'run';
  at: string;
  time: string;
  title: string;
  message: string;
  link: string | null;
  unread: boolean;
  status: { status: string; label: string } | null;
}

/**
 * Today, in time order like a trader's blotter: the next scheduled run on
 * top, then what ran (trading runs, with their orders and fills) and what
 * it decided (the signals from the strategies you follow), newest first.
 */
@Component({
  selector: 'app-signals-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, LoadingState, EmptyState, ErrorState, StatusPill],
  template: `
    <section class="panel" aria-labelledby="home-signals">
      <div class="panel-head">
        <h2 id="home-signals">Today's signals and runs</h2>
        @if (unread().length) {
          <button type="button" class="btn btn-ghost" [disabled]="marking()" (click)="markRead()">
            Mark all read
          </button>
        }
      </div>
      @if (feed.error(); as err) {
        <app-error-state title="Could not load signals" [error]="err" (retry)="feed.reload()" />
      } @else if (!feed.hasValue() || !day.settled()) {
        <!-- Waits for the schedule too, so the next run does not push the list down. -->
        <app-loading-state label="Loading signals" [rows]="3" />
      } @else if (rows().length === 0 && !next()) {
        <app-empty-state
          title="No signals today"
          message="Signals from the strategies you follow show here after each daily run."
        >
          <a routerLink="/strategies" class="btn">Find strategies to follow</a>
        </app-empty-state>
      } @else {
        <ol class="blotter">
          @if (next(); as n) {
            <li class="row next">
              <span class="time num">
                @if (n.day) {
                  <span class="weekday">{{ n.day }}</span>
                }
                {{ n.time }}</span
              >
              <span class="node" aria-hidden="true"></span>
              <div class="text">
                <span class="title">Next: {{ n.label }}</span>
                <p class="message">
                  Runs in <span class="num">{{ n.in }}</span>
                </p>
              </div>
            </li>
          }
          @for (r of rows(); track r.key) {
            <li class="row" [attr.data-kind]="r.kind" [class.unread]="r.unread">
              <span class="time num">{{ r.time }}</span>
              <span class="node" aria-hidden="true"></span>
              <div class="text">
                @if (r.unread) {
                  <span class="visually-hidden">New.</span>
                }
                @if (r.link; as href) {
                  <a [routerLink]="href" class="title">{{ r.title }}</a>
                } @else {
                  <span class="title">{{ r.title }}</span>
                }
                <p class="message">{{ r.message }}</p>
              </div>
              @if (r.status; as st) {
                <app-status-pill class="status" [status]="st.status" [label]="st.label" />
              }
            </li>
          }
          @if (signals().length === 0) {
            <li class="row quiet">
              <span class="time"></span>
              <span class="node" aria-hidden="true"></span>
              <p class="message">No signals today.</p>
            </li>
          }
        </ol>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .blotter {
      display: grid;
      margin: 0;
      padding: var(--space-2) var(--space-4) var(--space-3);
      list-style: none;
    }
    .row {
      position: relative;
      display: grid;
      grid-template-columns: 3.25rem 14px minmax(0, 1fr) auto;
      gap: 0 var(--space-3);
      align-items: start;
      padding: var(--space-2) 0;
    }
    /* The spine: a rule down the node column joins the day's events. */
    .row::before {
      content: '';
      position: absolute;
      top: 0;
      bottom: 0;
      left: calc(3.25rem + var(--space-3) + 6px);
      width: 2px;
      background: var(--color-border);
    }
    .row:first-child::before {
      top: 1.1rem;
    }
    .row:last-child::before {
      bottom: calc(100% - 1.1rem);
    }
    .weekday {
      display: block;
    }
    .time {
      padding-top: 2px;
      color: var(--color-ink-3);
      text-align: right;
    }
    .node {
      position: relative;
      z-index: 1;
      width: 14px;
      height: 14px;
      margin-top: 3px;
      border: 2px solid var(--color-ink-3);
      border-radius: var(--radius-xs);
      background: var(--color-surface);
    }
    .row[data-kind='signal'] .node {
      border-color: var(--color-accent);
      transform: rotate(45deg) scale(0.8);
    }
    .row.unread .node {
      background: var(--color-accent);
    }
    .row[data-kind='run'] .node {
      border-color: var(--color-ink);
      background: var(--color-ink);
    }
    .next .node {
      border-style: dashed;
      border-radius: 50%;
      border-color: var(--color-accent);
    }
    .next .title {
      color: var(--color-accent);
    }
    .quiet .node {
      width: 8px;
      height: 8px;
      margin: 7px 3px 0;
      border-width: 0;
      background: var(--color-border-strong);
      border-radius: 50%;
    }
    .text {
      min-width: 0;
    }
    .title {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    /* The whole row is the link's target (44px at least). */
    .row:has(a.title) {
      min-height: var(--touch-min);
    }
    a.title::after {
      content: '';
      position: absolute;
      inset: 0;
    }
    a.title:hover {
      text-decoration-thickness: 2px;
    }
    .message {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .status {
      margin-top: 2px;
    }
    @media (max-width: 767.98px) {
      .row {
        grid-template-columns: 3.25rem 14px minmax(0, 1fr);
      }
      .status {
        grid-column: 3;
        justify-self: start;
        margin-top: var(--space-1);
      }
    }
  `,
})
export class SignalsCard {
  private readonly api = inject(NotificationsService);
  private readonly ticksApi = inject(TicksService);
  protected readonly day = inject(TradingDayService);
  private readonly watch = inject(WatchlistContextService);
  private readonly counter = inject(NotificationFeedService);

  protected readonly feed = resource({ loader: () => this.api.feed({ limit: FEED_LIMIT }) });
  /** Runs are the second half of the story; if they fail, signals still show. */
  protected readonly ticks = resource({
    params: () => ({ done: this.ticksApi.finished() }),
    loader: async () => {
      try {
        return (await this.ticksApi.list({ limit: TICKS_LIMIT })).items;
      } catch {
        return [];
      }
    },
  });

  /** Fresh through the session: every minute, and when a trading run starts (UX-12). */
  protected readonly auto = autoRefresh(() => [this.feed, this.ticks], {
    triggers: [this.day.runsPassed],
  });

  private readonly now = signal(Date.now());

  /** Today's signals, narrowed to the picked watchlist (if any). */
  protected readonly signals = computed(() => {
    if (!this.feed.hasValue()) return [];
    const tickers = this.watch.tickers();
    return todaysSignals(this.feed.value().items).filter((s) => {
      const ticker = signalTicker(s.title);
      return !tickers || !ticker || tickers.has(ticker);
    });
  });
  protected readonly unread = computed(() => this.signals().filter((s) => !s.read_at));
  protected readonly marking = signal(false);

  protected readonly rows = computed<BlotterRow[]>(() => {
    const signals: BlotterRow[] = this.signals().map((s) => ({
      key: `s${s.id}`,
      kind: 'signal',
      at: s.created_at,
      time: formatTime(s.created_at),
      title: s.title,
      message: s.message,
      link: appLink(s.deep_link),
      unread: !s.read_at,
      status: null,
    }));
    const runs: BlotterRow[] = todaysRuns(this.ticks.hasValue() ? this.ticks.value() : []).map(
      (r) => ({
        key: `t${r.id}`,
        kind: 'run',
        at: r.finished_at ?? r.started_at,
        time: formatTime(r.finished_at ?? r.started_at),
        title: RUN_TITLE[r.status],
        message: runDetail(r),
        link: `/orders/ticks/${r.id}`,
        unread: false,
        status: RUN_WORDS[r.status],
      }),
    );
    return [...signals, ...runs].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  });

  protected readonly next = computed(() => {
    const now = this.now();
    // The run that trades, never the earliest system job (UX-08).
    const job = nextTradingRun(this.day.jobs(), now);
    if (!job?.next_run_at) return null;
    return {
      label: jobLabel(job),
      time: formatTime(job.next_run_at),
      // A run on another day carries its weekday, so 22:45 never reads as tonight.
      day:
        isoDay(new Date(job.next_run_at)) === isoDay(new Date(now))
          ? null
          : formatWeekday(job.next_run_at),
      in: countdown(Date.parse(job.next_run_at) - now),
    };
  });

  constructor() {
    const clock = setInterval(() => this.now.set(Date.now()), 1000);
    inject(DestroyRef).onDestroy(() => clearInterval(clock));
  }

  protected async markRead(): Promise<void> {
    this.marking.set(true);
    try {
      // Through the feed service, so the bell's count follows (UX-32).
      await this.counter.markRead(this.unread().map((s) => s.id));
      this.feed.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.marking.set(false);
    }
  }
}
