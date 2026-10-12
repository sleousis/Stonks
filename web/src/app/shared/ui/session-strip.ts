import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  InjectionToken,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { MarketSessionsView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { formatTime, formatWeekday } from '../../core/format/format';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { StopTradingService } from '../../core/halts/stop-trading.service';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { jobLabel, nextTradingRun } from '../../core/schedule/job-labels';
import { DaySpine, daySpine } from './day-spine';
import { KillSheet } from './kill-sheet';
import { StopTradingVisibility } from '../../shell/stop-visibility';
import { PortfolioPicker } from './portfolio-picker';
import { StripHaltActions } from './strip-halt-actions';

/** How often the rail re-reads the schedule (the countdown ticks every second). */
export const SCHEDULE_POLL_MS = new InjectionToken<number>('SCHEDULE_POLL_MS', {
  providedIn: 'root',
  factory: () => 5 * 60_000,
});

export { nextJob } from '../../core/schedule/job-labels';

/** After-hours trading runs four hours past the close. */
const POST_MS = 4 * 3_600_000;

/** "2h 05m", "4m 09s", "12s", "now". */
export function countdown(ms: number): string {
  if (ms <= 0) return 'now';
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86_400);
  const h = Math.floor((s % 86_400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const two = (n: number) => String(n).padStart(2, '0');
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${two(m)}m`;
  if (m) return `${m}m ${two(sec)}s`;
  return `${sec}s`;
}

/** Where the trading day stands, for the rail. */
export interface SessionPhase {
  phase: 'pre' | 'open' | 'post' | 'closed';
  /** "Pre-market", "Market open", "After hours", "Market closed". */
  label: string;
  /** What happens next and when: "Opens", "Closes", with its time. */
  event: string;
  at: string;
  /** Milliseconds until that event. */
  inMs: number;
  /** Today's session, or null on a day without one. */
  track: {
    preOpen: string;
    open: string;
    close: string;
    /** 0..1 from pre-open to close; where the open mark and "now" sit. */
    openAt: number;
    now: number;
  } | null;
}

/** The phase of the trading day at `now`, from the schedule's market sessions. */
export function sessionPhase(market: MarketSessionsView, now: number): SessionPhase {
  const today = market.today;
  const day = today
    ? {
        pre: Date.parse(today.pre_open),
        open: Date.parse(today.open),
        close: Date.parse(today.close),
      }
    : null;
  const track =
    today && day && day.close > day.pre
      ? {
          preOpen: formatTime(today.pre_open),
          open: formatTime(today.open),
          close: formatTime(today.close),
          openAt: (day.open - day.pre) / (day.close - day.pre),
          now: Math.min(1, Math.max(0, (now - day.pre) / (day.close - day.pre))),
        }
      : null;
  const nextOpen = (phase: 'closed' | 'post', label: string): SessionPhase => {
    const future = day && now < day.open ? today! : market.next;
    const openAt = Date.parse(future.open);
    const sameDay = day && now < day.open;
    return {
      phase,
      label,
      event: 'Opens',
      at: sameDay
        ? formatTime(future.open)
        : `${formatWeekday(future.open)} ${formatTime(future.open)}`,
      inMs: openAt - now,
      track,
    };
  };
  if (day && now >= day.close && now < day.close + POST_MS) {
    return nextOpen('post', 'After hours');
  }
  if (!day || now >= day.close) return nextOpen('closed', 'Market closed');
  if (now >= day.open) {
    return {
      phase: 'open',
      label: 'Market open',
      event: 'Closes',
      at: formatTime(today!.close),
      inMs: day.close - now,
      track,
    };
  }
  if (now >= day.pre) {
    return {
      phase: 'pre',
      label: 'Pre-market',
      event: 'Opens',
      at: formatTime(today!.open),
      inMs: day.open - now,
      track,
    };
  }
  return nextOpen('closed', 'Market closed');
}

/** "16:45" for today, "Mon 16:45" for another day. */
function runTime(iso: string, now: number): string {
  const sameDay =
    Math.abs(Date.parse(iso) - now) < 86_400_000 &&
    formatWeekday(iso) === formatWeekday(new Date(now).toISOString());
  return sameDay ? formatTime(iso) : `${formatWeekday(iso)} ${formatTime(iso)}`;
}

/**
 * The session rail under the chrome bar, on every page: the portfolio
 * switcher with its PAPER or LIVE stamp, the market session chip (closed,
 * pre-market, open, after hours, halted), the day spine over the local day
 * (desktop), the next trading run with a live countdown (UX-08) and Stop
 * trading (UX-01): one tap opens the kill switch sheet, with a brass ring
 * when the shown portfolio trades real money. While trading is halted the
 * rail turns red and a halt band under it says why, with Resume.
 */
@Component({
  selector: 'app-session-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PortfolioPicker, KillSheet, StripHaltActions, DaySpine],
  template: `
    @let halt = halted();
    @let day = phase();
    <div aria-live="polite">
      @if (
        halt || next() || noRun() || day || waiting() || portfolios.options().length || canKill()
      ) {
        <div class="strip" [attr.data-tone]="halt?.tone ?? 'calm'">
          <div class="rail">
            @if (portfolios.options().length) {
              <span class="pick"><app-portfolio-picker /></span>
            }
            @if (day) {
              <p
                class="rail-chip phase"
                [attr.data-phase]="halt ? 'halted' : day.phase"
                [attr.data-day]="day.phase"
              >
                <span class="glyph" aria-hidden="true"></span>
                <span class="phase-label">{{ halt ? 'Halted' : day.label }}</span>
                <span class="meta phase-event">
                  {{ day.event }} <span class="num">{{ day.at }}</span>
                </span>
              </p>
            } @else if (waiting()) {
              <!-- Holds the phase's place until the schedule is read, so pages do not jump. -->
              <p class="rail-chip phase hold" aria-hidden="true">
                <span class="glyph"></span><span class="phase-label">Market</span>
                <span class="meta">Opens <span class="num">00:00</span></span>
              </p>
            }
            @if (spine(); as s) {
              <app-day-spine class="spine" [spine]="s" [halted]="!!halt" />
            } @else {
              <span class="gap"></span>
            }
            @if (next(); as n) {
              <a
                class="rail-chip next"
                [routerLink]="schedulePath()"
                [attr.aria-label]="nextLabel()"
                [attr.title]="n.trigger"
              >
                <span class="job">Trading run</span>
                <span class="at num meta">{{ n.at }}</span>
                <span class="in" aria-hidden="true">in</span>
                <span class="clock num" aria-hidden="true">{{ n.in }}</span>
              </a>
            } @else if (noRun()) {
              <a class="rail-chip next none" [routerLink]="schedulePath()"
                >No trading run scheduled</a
              >
            } @else if (waiting()) {
              <span class="rail-chip next hold" aria-hidden="true">
                <span class="job">Trading run</span>
                <span class="clock num">0h 00m</span>
              </span>
            }
            @if (canKill()) {
              @if (killOn()) {
                @if (isAdmin()) {
                  <a
                    class="stop resume"
                    routerLink="/ops/halts"
                    aria-label="Resume trading on the Halts page"
                  >
                    <span class="stop-mark" aria-hidden="true"></span>
                    Resume
                  </a>
                }
              } @else {
                <button
                  type="button"
                  class="stop"
                  [class.live]="live()"
                  aria-haspopup="dialog"
                  aria-label="Stop trading"
                  (click)="sheetOpen.set(true)"
                >
                  <span class="stop-mark" aria-hidden="true"></span>
                  <span>Stop<span class="stop-long" aria-hidden="true"> trading</span></span>
                </button>
              }
            }
          </div>
          @if (halt) {
            <div class="halt" role="region" aria-label="Trading halted">
              <svg
                class="warn"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                stroke-width="1.6"
                aria-hidden="true"
              >
                <path d="M8 1.8 15 14H1z" stroke-linejoin="round" />
                <path d="M8 6.2v3.6M8 11.6v.4" stroke-linecap="round" />
              </svg>
              <p class="text">
                <strong>{{ halt.title }}</strong> <span>{{ halt.text }}</span>
              </p>
              <span class="halt-actions">
                @if (isAdmin()) {
                  <a class="strip-link" routerLink="/ops/halts">Review halts</a>
                } @else {
                  <app-strip-halt-actions />
                }
              </span>
            </div>
          }
        </div>
      }
    </div>
    @if (canKill()) {
      <!-- Loaded once the page is idle (or at the first tap), so it is ready before it is needed. -->
      @defer (on idle; when sheetOpen()) {
        <app-kill-sheet [(open)]="sheetOpen" />
      }
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
    }
    .strip {
      --stamp-ink: var(--chrome-ink);
      border-top: 1px solid var(--chrome-rule);
      background: var(--chrome);
      color: var(--chrome-ink);
    }
    .strip[data-tone='kill'],
    .strip[data-tone='halt'] {
      border-top-color: var(--halt-rule);
      background: var(--halt-band);
    }
    .rail {
      display: flex;
      align-items: center;
      gap: var(--space-3);
      max-width: var(--content-max);
      min-height: var(--rail-h);
      margin: 0 auto;
      padding: var(--space-1) var(--gutter);
      padding-left: calc(var(--gutter) + env(safe-area-inset-left));
      padding-right: calc(var(--gutter) + env(safe-area-inset-right));
    }
    .pick {
      display: inline-flex;
      min-width: 0;
    }
    .phase {
      margin: 0;
    }
    .glyph {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      border: 1.5px solid var(--chrome-muted);
    }
    .phase[data-phase='open'] .glyph {
      border-color: #3fd29a;
      background: #3fd29a;
    }
    .phase[data-phase='pre'] .glyph,
    .phase[data-phase='post'] .glyph {
      border-color: #3fd29a;
      background: linear-gradient(90deg, #3fd29a 50%, transparent 50%);
    }
    .phase[data-phase='halted'] {
      border-color: var(--halt-rule);
      background: #5a1418;
    }
    .phase[data-phase='halted'] .glyph {
      border-color: var(--on-halt);
      border-radius: 1px;
      background: var(--on-halt);
    }
    .strip:not([data-tone='calm']) .meta {
      color: var(--on-halt-muted);
    }
    .gap {
      flex: 1 1 auto;
    }
    .hold {
      visibility: hidden;
    }
    .next {
      background: none;
    }
    .next:hover .job,
    .next.none:hover {
      text-decoration: underline;
    }
    .clock {
      color: var(--chrome-accent);
      font-weight: var(--weight-semibold);
    }
    .strip:not([data-tone='calm']) .clock {
      color: var(--on-halt);
    }
    .in {
      color: var(--chrome-muted);
      font-weight: var(--weight-medium);
    }
    /* With the spine on screen the run's time is on it already. */
    @include bp.from-desktop {
      .spine ~ .next .at {
        display: none;
      }
    }

    /* Stop trading: outlined red on the ink, a stop square. Brass ring for real money. */
    .stop {
      display: inline-flex;
      flex: none;
      align-items: center;
      gap: var(--space-2);
      min-height: 34px;
      padding: 0 var(--space-3);
      border: 1px solid var(--chrome-stop-line);
      border-radius: var(--r-ctl);
      background: none;
      color: var(--chrome-stop);
      font: inherit;
      font-size: 0.8125rem;
      font-weight: var(--weight-semibold);
      white-space: nowrap;
      text-decoration: none;
      cursor: pointer;
    }
    .stop:hover {
      background: #2a1416;
    }
    .stop.live {
      box-shadow:
        0 0 0 2px var(--chrome),
        0 0 0 4px var(--brass-fill);
    }
    .stop-mark {
      width: 9px;
      height: 9px;
      flex: none;
      border-radius: 2px;
      background: currentColor;
    }
    .stop.resume {
      border-color: var(--on-halt);
      color: var(--on-halt);
    }
    .stop.resume .stop-mark {
      width: 0;
      height: 0;
      border-radius: 0;
      background: none;
      border-block: 5px solid transparent;
      border-left: 9px solid currentColor;
    }

    /* The halt band: full width under the rail. */
    .halt {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      max-width: var(--content-max);
      min-height: 44px;
      margin: 0 auto;
      padding: var(--space-2) var(--gutter);
      border-top: 1px solid var(--halt-rule);
      color: var(--on-halt);
      font-size: var(--text-md);
    }
    .warn {
      flex: none;
      width: 16px;
      height: 16px;
    }
    .text {
      flex: 1 1 18rem;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .text strong {
      margin-right: 0.35em;
      font-weight: var(--weight-bold);
    }
    .halt-actions {
      display: inline-flex;
      gap: var(--space-2);
    }
    .strip-link {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 0 var(--space-3);
      border: 1px solid var(--on-halt);
      border-radius: var(--r-ctl);
      color: var(--on-halt);
      font-weight: var(--weight-semibold);
      text-decoration: none;
    }
    @include bp.below-desktop {
      .spine {
        display: none;
      }
    }
    @include bp.coarse {
      .strip-link,
      .stop {
        min-height: var(--touch-min);
      }
    }
    /* Phones: line 1 the portfolio and a red Stop, line 2 the session and run chips. */
    @include bp.phone {
      .rail {
        flex-wrap: wrap;
        gap: var(--space-2);
        padding: var(--space-2) var(--space-4) 10px;
      }
      /* Wide enough that only Stop fits beside it: the chips wrap below. */
      .pick {
        order: 1;
        flex: 1 0 calc(100% - 7rem);
        min-width: 0;
      }
      .stop {
        order: 2;
        min-height: var(--touch-min);
        margin-left: auto;
      }
      .stop:not(.resume) {
        border-color: var(--chrome-stop-line);
        background: #a6242b;
        color: var(--on-halt);
      }
      .stop-long {
        display: none;
      }
      .phase {
        order: 3;
      }
      .phase-event {
        display: none;
      }
      .gap {
        display: none;
      }
      .next {
        order: 4;
      }
      .next .at {
        display: none;
      }
      .halt {
        padding: var(--space-3) var(--space-4);
      }
      .halt-actions,
      .halt-actions > *,
      .strip-link {
        width: 100%;
      }
      .strip-link {
        justify-content: center;
        min-height: var(--touch-min);
      }
    }
  `,
})
export class SessionStrip {
  private readonly halts = inject(HaltStateService);
  private readonly pollMs = inject(SCHEDULE_POLL_MS);
  private readonly session = inject(SessionService);
  protected readonly portfolios = inject(PortfolioContextService);

  private readonly day = inject(TradingDayService);
  private readonly jobs = this.day.jobs;
  private readonly market = this.day.market;
  private readonly now = signal(Date.now());

  protected readonly halted = this.halts.summary;
  protected readonly killOn = this.halts.killOn;
  /**
   * Traders and admins can stop trading; viewers never see the control. A
   * trader sees it once something trades for them (FR-06).
   */
  protected readonly canKill = inject(StopTradingVisibility).show;
  /** Real money on the shown portfolio: the Stop control wears the brass ring. */
  protected readonly live = this.portfolios.live;
  protected readonly sheetOpen = inject(StopTradingService).open;

  protected readonly phase = computed(() => {
    const market = this.market();
    return market ? sessionPhase(market, this.now()) : null;
  });

  /** The next trading run: the countdown that matters to a trader (UX-08). */
  private readonly nextRun = computed(() => nextTradingRun(this.jobs(), this.now()));

  protected readonly next = computed(() => {
    const now = this.now();
    const job = this.nextRun();
    if (!job?.next_run_at) return null;
    return {
      label: jobLabel(job),
      at: runTime(job.next_run_at, now),
      in: countdown(Date.parse(job.next_run_at) - now),
      trigger: job.trigger_text || null,
    };
  });

  /** The day spine: the session's hours over the local day, now and the next run. */
  protected readonly spine = computed(() => {
    const market = this.market();
    return market ? daySpine(market, this.now(), this.nextRun()?.next_run_at ?? null) : null;
  });

  /** The schedule loaded and holds no trading run: say so plainly. */
  protected readonly noRun = computed(() => this.day.loaded() && !this.next());
  /** Signed in and the schedule not read yet: hold the rail's height. */
  protected readonly waiting = computed(() => this.session.canRead() && !this.day.settled());

  /** Traders and admins can stop trading; admins also reach the Halts and Schedule pages. */
  protected readonly isAdmin = computed(() => this.session.isAdmin());

  /** Admins open the schedule; everyone else opens Today, where the next run leads the day (F11). */
  protected readonly schedulePath = computed(() => (this.isAdmin() ? '/ops/schedule' : '/'));

  /** Screen readers get a stable label; the ticking clock is hidden from them. */
  protected readonly nextLabel = computed(() => {
    const n = this.next();
    if (!n) return null;
    return `Next trading run at ${n.at}. ${this.isAdmin() ? 'Open the schedule.' : 'Open Today.'}`;
  });

  constructor() {
    // Nothing is asked of the API before sign-in (BUG-1).
    effect(() => {
      if (this.session.canRead()) {
        untracked(() => {
          void this.load();
          void this.portfolios.load();
        });
      }
    });
    const clock = setInterval(() => this.now.set(Date.now()), 1000);
    const poll = this.pollMs > 0 ? setInterval(() => void this.load(), this.pollMs) : null;
    inject(DestroyRef).onDestroy(() => {
      clearInterval(clock);
      if (poll) clearInterval(poll);
    });
  }

  private async load(): Promise<void> {
    if (!this.session.canRead()) return;
    await this.day.load();
  }
}
