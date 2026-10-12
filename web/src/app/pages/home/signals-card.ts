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
/** Today shows the latest signals only; the rest are in Notifications. */
export const SIGNALS_SHOWN = 8;

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
 * (M15), or null when none of them took part. The API scopes the counts to
 * the reader's books: `orders_placed` and `fills` are null (absent) when
 * none of them traded, so another book's totals never show as the reader's.
 */
export function ownRunCounts(run: TickRun): { orders: number; fills: number } | null {
  const s = run.summary;
  if (!s || (s.orders_placed == null && s.fills == null)) return null;
  return { orders: s.orders_placed ?? 0, fills: s.fills ?? 0 };
}

/**
 * One line under a trading run: "3 orders, 3 fills" in your portfolios, or
 * its error. Nothing when none of your portfolios traded: the title
 * ("Trading run finished") says it all.
 */
export function runDetail(run: TickRun): string {
  const s = run.summary;
  if (run.status === 'running') return 'Deciding and placing orders now.';
  if (s?.error) return s.error;
  const own = ownRunCounts(run);
  if (!own) return '';
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;
  return `${plural(own.orders, 'order')}, ${plural(own.fills, 'fill')} in your portfolios.`;
}

/**
 * Runs that matter to the reader get their own row: one that traded in
 * their portfolios, failed, had problems or is still going. Quiet runs
 * fold into one line (FR-04).
 */
export function runIsOwnRow(run: TickRun): boolean {
  if (run.status !== 'ok') return true;
  const own = ownRunCounts(run);
  // Your portfolios took part but nothing traded: nothing for you either.
  return own !== null && (own.orders > 0 || own.fills > 0);
}

/** "10 trading runs finished, nothing for you". */
export function quietRunsTitle(n: number): string {
  return `${n} trading ${n === 1 ? 'run' : 'runs'} finished, nothing for you`;
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
  /** The ticker a signal names, shown as a tag. */
  ticker?: string | null;
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
    <section class="page-section" aria-labelledby="home-signals">
      <header class="section-head">
        <h2 id="home-signals">Signals and runs</h2>
        <span class="head-links">
          @if (unread().length) {
            <button
              type="button"
              class="btn btn-ghost btn-compact"
              [disabled]="marking()"
              (click)="markRead()"
            >
              Mark all read
            </button>
          }
          <a routerLink="/notifications" class="text-link">All signals</a>
        </span>
      </header>
      @if (feed.error(); as err) {
        <div class="section-body">
          <app-error-state title="Could not load signals" [error]="err" (retry)="feed.reload()" />
        </div>
      } @else if (!feed.hasValue() || !day.settled()) {
        <!-- Waits for the schedule too, so the next run does not push the list down. -->
        <div class="section-body">
          <app-loading-state label="Loading signals" [rows]="3" />
        </div>
      } @else if (rows().length === 0 && !next()) {
        <div class="section-body">
          <app-empty-state
            title="No signals today"
            message="Signals from the strategies you follow show here after each daily run."
          >
            <a routerLink="/strategies" class="btn">Find strategies to follow</a>
          </app-empty-state>
        </div>
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
                @if (r.message) {
                  <p class="message">{{ r.message }}</p>
                }
                @if (r.ticker; as t) {
                  <span class="ticker-tag">{{ t }}</span>
                }
                @if (r.status; as st) {
                  <app-status-pill class="status" [status]="st.status" [label]="st.label" />
                }
              </div>
            </li>
          }
          @if (signals().length > shownLimit) {
            <li class="row more">
              <span class="time"></span>
              <span class="node" aria-hidden="true"></span>
              <a routerLink="/notifications" class="title"
                >See all {{ signals().length }} in Notifications</a
              >
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
    .head-links {
      display: inline-flex;
      align-items: center;
      gap: var(--space-3);
    }
    /* A timeline: mono times on the left, a 1px rail with 8px nodes. */
    .blotter {
      display: grid;
      margin: 0;
      padding: var(--space-4) 0 0;
      list-style: none;
    }
    .row {
      position: relative;
      display: grid;
      grid-template-columns: 4.25rem 8px minmax(0, 1fr);
      gap: 0 var(--space-4);
      align-items: start;
      padding-bottom: var(--space-5);
    }
    .row::before {
      content: '';
      position: absolute;
      top: 0;
      bottom: 0;
      left: calc(4.25rem + var(--space-4) + 3.5px);
      width: 1px;
      background: var(--rule);
    }
    .row:first-child::before {
      top: 8px;
    }
    .row:last-child::before {
      bottom: calc(100% - 8px);
    }
    .row:last-child {
      padding-bottom: 0;
    }
    .weekday {
      margin-right: 4px;
    }
    .time {
      padding-top: 2px;
      color: var(--muted);
      font-size: var(--text-xs);
      white-space: nowrap;
    }
    .node {
      position: relative;
      z-index: 1;
      width: 8px;
      height: 8px;
      margin-top: 6px;
      border-radius: 50%;
      background: var(--ink-2);
      box-shadow: 0 0 0 3px var(--bg);
    }
    .row[data-kind='signal'] .node {
      background: var(--bg);
      box-shadow:
        inset 0 0 0 1.5px var(--accent),
        0 0 0 3px var(--bg);
    }
    .row.unread .node {
      background: var(--accent);
    }
    /* The next run: an accent ring. */
    .next .node {
      background: var(--bg);
      box-shadow:
        inset 0 0 0 2px var(--accent),
        0 0 0 3px var(--bg);
    }
    .next .title {
      color: var(--accent);
    }
    .quiet .node,
    .more .node {
      background: var(--rule-control);
    }
    .text {
      display: grid;
      justify-items: start;
      gap: 2px;
      min-width: 0;
    }
    .title {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      font-size: var(--text-md);
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    /* A 44px target that still lays out as one 20px line (FR-23). */
    a.title {
      min-height: var(--touch-min);
      margin-block: calc((20px - var(--touch-min)) / 2);
      color: var(--ink);
      text-decoration: none;
    }
    a.title::after {
      content: '';
      position: absolute;
      inset: 0;
    }
    a.title:hover,
    a.title:focus-visible {
      text-decoration: underline;
    }
    .more a.title {
      color: var(--accent);
      font-weight: var(--weight-medium);
    }
    .row:has(a.title) {
      min-height: var(--touch-min);
    }
    .message {
      color: var(--muted);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .ticker-tag,
    .status {
      margin-top: var(--space-1);
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
  protected readonly shownLimit = SIGNALS_SHOWN;
  protected readonly marking = signal(false);

  protected readonly rows = computed<BlotterRow[]>(() => {
    const latest = [...this.signals()]
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
      .slice(0, SIGNALS_SHOWN);
    const signals: BlotterRow[] = latest.map((s) => ({
      key: `s${s.id}`,
      kind: 'signal',
      at: s.created_at,
      time: formatTime(s.created_at),
      title: s.title,
      message: s.message,
      link: appLink(s.deep_link),
      unread: !s.read_at,
      status: null,
      ticker: signalTicker(s.title),
    }));
    const today = todaysRuns(this.ticks.hasValue() ? this.ticks.value() : []);
    const runs: BlotterRow[] = today.filter(runIsOwnRow).map((r) => ({
      key: `t${r.id}`,
      kind: 'run',
      at: r.finished_at ?? r.started_at,
      time: formatTime(r.finished_at ?? r.started_at),
      title: RUN_TITLE[r.status],
      message: runDetail(r),
      link: `/orders/ticks/${r.id}`,
      unread: false,
      status: RUN_WORDS[r.status],
    }));
    const quiet = today.filter((r) => !runIsOwnRow(r));
    if (quiet.length) {
      const at = quiet
        .map((r) => r.finished_at ?? r.started_at)
        .reduce((a, b) => (Date.parse(a) >= Date.parse(b) ? a : b));
      runs.push({
        key: 'quiet-runs',
        kind: 'run',
        at,
        time: formatTime(at),
        title: quietRunsTitle(quiet.length),
        message: '',
        link: '/orders/ticks',
        unread: false,
        status: null,
      });
    }
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
