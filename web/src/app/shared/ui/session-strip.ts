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
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { haltSummary } from '../../core/halts/halt-view';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { humanize } from './param-form/param-spec';
import { nextJob } from '../../core/schedule/job-labels';
import { PortfolioPicker } from './portfolio-picker';

/** How often the strip re-reads the schedule (the countdown ticks every second). */
export const SCHEDULE_POLL_MS = new InjectionToken<number>('SCHEDULE_POLL_MS', {
  providedIn: 'root',
  factory: () => 5 * 60_000,
});

export { nextJob } from '../../core/schedule/job-labels';

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

/** Where the trading day stands, for the strip. */
export interface SessionPhase {
  phase: 'pre' | 'open' | 'closed';
  /** "Pre-open", "Market open", "Market closed". */
  label: string;
  /** What happens next and when: "Opens", "Closes", with its time. */
  event: string;
  at: string;
  /** Milliseconds until that event. */
  inMs: number;
  /** Today's session for the track, or null on a day without one. */
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
  const nextOpen = (): SessionPhase => {
    const future = day && now < day.open ? today! : market.next;
    const openAt = Date.parse(future.open);
    const sameDay = day && now < day.open;
    return {
      phase: 'closed',
      label: 'Market closed',
      event: 'Opens',
      at: sameDay
        ? formatTime(future.open)
        : `${formatWeekday(future.open)} ${formatTime(future.open)}`,
      inMs: openAt - now,
      track,
    };
  };
  if (!day || now >= day.close) return nextOpen();
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
      label: 'Pre-open',
      event: 'Opens',
      at: formatTime(today!.open),
      inMs: day.open - now,
      track,
    };
  }
  return nextOpen();
}

/**
 * The trading day, on every page: the market phase (pre-open, open, closed)
 * with a track from pre-open to the close, the next scheduled run with a
 * live countdown, and the halt state. It turns red while a kill switch is
 * on (amber for a breaker) and links to the halts page. It also holds the
 * portfolio picker when the user has more than one portfolio.
 */
@Component({
  selector: 'app-session-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PortfolioPicker],
  template: `
    @let halt = halted();
    @let day = phase();
    <div aria-live="polite">
      @if (halt || next() || day || portfolios.hasChoice()) {
        <div class="strip" [attr.data-tone]="halt?.tone ?? 'calm'">
          @if (halt) {
            <p class="halt" role="region" aria-label="Trading halted">
              <span class="mark" aria-hidden="true"></span>
              <span class="text"
                ><strong>{{ halt.title }}</strong> {{ halt.text }}</span
              >
              <a class="strip-link" routerLink="/ops/halts">Review halts</a>
            </p>
          }
          <div class="row">
            @if (day) {
              <p class="phase" [attr.data-phase]="day.phase">
                <span class="lamp" aria-hidden="true"></span>
                <span class="phase-label">{{ day.label }}</span>
                <span class="phase-event muted">
                  {{ day.event }} <span class="num">{{ day.at }}</span>
                </span>
              </p>
              @if (day.track; as t) {
                <div class="track" aria-hidden="true">
                  <span class="t-time num">{{ t.preOpen }}</span>
                  <span class="rail">
                    <span class="pre" [style.width.%]="t.openAt * 100"></span>
                    <span class="now" [style.left.%]="t.now * 100"></span>
                  </span>
                  <span class="t-time num">{{ t.close }}</span>
                </div>
              }
            }
            <app-portfolio-picker />
            @if (next(); as n) {
              <a
                class="next"
                routerLink="/ops/schedule"
                [attr.aria-label]="nextLabel()"
                [attr.title]="n.trigger"
              >
                <span class="muted">Next</span>
                <span class="job">{{ n.label }}</span>
                <span class="at num muted">{{ n.at }}</span>
                <span class="clock num" aria-hidden="true">{{ n.in }}</span>
              </a>
            }
          </div>
        </div>
      }
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
    }
    .strip {
      --tone: var(--color-border-strong);
      --tone-soft: var(--color-surface);
      margin-bottom: var(--space-5);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--tone-soft);
      font-size: var(--text-sm);
      box-shadow: var(--shadow-1);
      overflow: hidden;
    }
    .strip[data-tone='kill'] {
      --tone: var(--color-loss);
      --tone-soft: var(--color-loss-soft);
      border: 2px solid var(--color-loss);
    }
    .strip[data-tone='halt'] {
      --tone: var(--color-warn);
      --tone-soft: var(--color-warn-soft);
      border: 2px solid var(--color-warn);
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-4);
      min-height: var(--strip-h);
      padding: var(--space-1) var(--space-3);
    }
    .halt {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-3);
      margin: 0;
      padding: var(--space-2) var(--space-3);
      background: var(--tone);
      color: var(--color-surface);
    }
    .halt .text {
      flex: 1 1 14rem;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .mark {
      width: 10px;
      height: 10px;
      flex: none;
      background: currentColor;
      transform: rotate(45deg);
    }
    .strip[data-tone='halt'] .mark {
      transform: none;
      width: 12px;
      clip-path: polygon(50% 0, 100% 100%, 0 100%);
    }
    .strip-link {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      color: inherit;
      font-weight: var(--weight-bold);
    }

    /* The market phase: a lamp, the phase in the display face, what is next. */
    .phase {
      display: inline-flex;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 0 var(--space-2);
      margin: 0;
    }
    .lamp {
      align-self: center;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      border: 1.5px solid var(--color-ink-3);
    }
    .phase[data-phase='open'] .lamp {
      border-color: var(--color-gain);
      background: var(--color-gain);
      box-shadow: 0 0 0 3px var(--color-gain-soft);
    }
    .phase[data-phase='pre'] .lamp {
      border-color: var(--color-accent);
      background: linear-gradient(90deg, var(--color-accent) 50%, transparent 50%);
    }
    .phase-label {
      font-family: var(--font-display);
      font-stretch: var(--display-stretch);
      font-size: var(--text-lg);
      font-weight: var(--weight-bold);
      letter-spacing: var(--tracking-display);
    }

    /* The day track: pre-open shaded, the open session plain, a tick at now. */
    .track {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      flex: 1 1 10rem;
      min-width: 8rem;
      max-width: 22rem;
      color: var(--color-ink-3);
      font-size: var(--text-xs);
    }
    .rail {
      position: relative;
      flex: 1;
      height: 6px;
      border-radius: var(--radius-xs);
      background: var(--color-surface-3);
    }
    .pre {
      position: absolute;
      inset: 0 auto 0 0;
      border-radius: var(--radius-xs) 0 0 var(--radius-xs);
      background: repeating-linear-gradient(
        -45deg,
        var(--color-border-strong) 0 2px,
        transparent 2px 5px
      );
    }
    .now {
      position: absolute;
      top: -4px;
      width: 2px;
      height: 14px;
      margin-left: -1px;
      border-radius: 1px;
      background: var(--color-ink);
      transition: left var(--dur-slow) var(--ease);
    }

    .next {
      display: inline-flex;
      align-items: baseline;
      gap: var(--space-2);
      min-height: 32px;
      margin-left: auto;
      align-items: center;
      color: inherit;
      text-decoration: none;
    }
    .next:hover .job {
      text-decoration: underline;
    }
    .job {
      font-weight: var(--weight-semibold);
    }
    .clock {
      min-width: 6.5ch;
      padding: 1px var(--space-2);
      border-radius: var(--radius-xs);
      background: var(--color-ink);
      color: var(--color-surface);
      text-align: center;
      font-weight: var(--weight-semibold);
    }
    @include bp.coarse {
      .strip-link,
      .next {
        min-height: var(--touch-min);
      }
    }
    @include bp.phone {
      .strip {
        margin-bottom: var(--space-4);
      }
      .strip-link,
      .next {
        min-height: var(--touch-min);
      }
      .strip-link {
        width: 100%;
      }
      .track {
        flex-basis: 100%;
        max-width: none;
        order: 3;
      }
      .at {
        display: none;
      }
      .next {
        margin-left: 0;
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

  protected readonly halted = computed(() => haltSummary(this.halts.active()));

  protected readonly phase = computed(() => {
    const market = this.market();
    return market ? sessionPhase(market, this.now()) : null;
  });

  protected readonly next = computed(() => {
    const now = this.now();
    const job = nextJob(this.jobs(), now);
    if (!job?.next_run_at) return null;
    return {
      label: humanize(job.name),
      at: formatTime(job.next_run_at),
      in: countdown(Date.parse(job.next_run_at) - now),
      trigger: job.trigger_text || null,
    };
  });

  /** Screen readers get a stable label; the ticking clock is hidden from them. */
  protected readonly nextLabel = computed(() => {
    const n = this.next();
    return n ? `Next scheduled run: ${n.label} at ${n.at}. Open the schedule.` : null;
  });

  constructor() {
    // Nothing is asked of the API before sign-in (BUG-1).
    effect(() => {
      if (this.session.canRead()) untracked(() => void this.load());
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
