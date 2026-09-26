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

import type { ScheduledJobView } from '../../api/models';
import { ScheduleService } from '../../api/schedule.service';
import { SessionService } from '../../core/auth/session.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { haltSummary } from '../../core/halts/halt-view';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { humanize } from './param-form/param-spec';
import { PortfolioPicker } from './portfolio-picker';

/** How often the strip re-reads the schedule (the countdown ticks every second). */
export const SCHEDULE_POLL_MS = new InjectionToken<number>('SCHEDULE_POLL_MS', {
  providedIn: 'root',
  factory: () => 5 * 60_000,
});

/** The job that fires next, from `GET /api/schedule`. */
export function nextJob(jobs: readonly ScheduledJobView[], now: number): ScheduledJobView | null {
  let best: ScheduledJobView | null = null;
  let bestAt = Infinity;
  for (const j of jobs) {
    const at = j.next_run_at ? Date.parse(j.next_run_at) : NaN;
    if (Number.isFinite(at) && at >= now - 60_000 && at < bestAt) {
      best = j;
      bestAt = at;
    }
  }
  return best;
}

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

/**
 * The slim strip at the top of every page: the next scheduled run with a
 * live countdown, and the halt state. It turns red while a kill switch is
 * on (amber for a breaker) and links to the halts page.
 *
 * It also holds the portfolio picker when the user has more than one.
 *
 * Owned here: the halt state. The trading-day part (pre-open, open, close)
 * waits for session times from the API.
 */
@Component({
  selector: 'app-session-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PortfolioPicker],
  template: `
    @let halt = halted();
    <div aria-live="polite">
      @if (halt || next() || portfolios.hasChoice()) {
        <div class="strip" [attr.data-tone]="halt?.tone ?? 'calm'">
          @if (halt) {
            <span class="mark" aria-hidden="true"></span>
            <p class="text" role="region" aria-label="Trading halted">
              <strong>{{ halt.title }}</strong> {{ halt.text }}
            </p>
            <a class="strip-link" routerLink="/ops/halts">Review halts</a>
          }
          <app-portfolio-picker />
          @if (next(); as n) {
            <a class="next" routerLink="/ops/schedule" [attr.aria-label]="nextLabel()">
              <span class="muted">Next</span>
              <span class="job">{{ n.label }}</span>
              <span class="clock num" aria-hidden="true">{{ n.in }}</span>
            </a>
          }
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
      margin-bottom: var(--space-4);
    }
    .strip {
      --tone: var(--color-border-strong);
      --tone-soft: var(--color-surface-2);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-3);
      min-height: 32px;
      padding: var(--space-1) var(--space-3);
      border: 1px solid var(--color-border);
      border-left: 4px solid var(--tone);
      border-radius: var(--radius-md);
      background: var(--tone-soft);
      font-size: var(--text-sm);
    }
    .strip[data-tone='kill'] {
      --tone: var(--color-loss);
      --tone-soft: var(--color-loss-soft);
      border-color: var(--color-loss);
    }
    .strip[data-tone='halt'] {
      --tone: var(--color-warn);
      --tone-soft: var(--color-warn-soft);
      border-color: var(--color-warn);
    }
    .mark {
      width: 10px;
      height: 10px;
      flex: none;
      background: var(--tone);
      transform: rotate(45deg);
    }
    .strip[data-tone='halt'] .mark {
      transform: none;
      width: 12px;
      clip-path: polygon(50% 0, 100% 100%, 0 100%);
    }
    .text {
      flex: 1 1 14rem;
      min-width: 0;
      margin: 0;
      overflow-wrap: anywhere;
    }
    strong {
      color: var(--tone);
    }
    .strip-link,
    .next {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: 32px;
      color: inherit;
    }
    .strip-link {
      font-weight: var(--weight-semibold);
    }
    .next {
      margin-left: auto;
      text-decoration: none;
    }
    .job {
      font-weight: var(--weight-medium);
    }
    .clock {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }
    @include bp.coarse {
      .strip-link,
      .next {
        min-height: var(--touch-min);
      }
    }
    @include bp.phone {
      .strip-link,
      .next {
        min-height: var(--touch-min);
      }
      .strip-link {
        width: 100%;
      }
    }
  `,
})
export class SessionStrip {
  private readonly schedule = inject(ScheduleService);
  private readonly halts = inject(HaltStateService);
  private readonly pollMs = inject(SCHEDULE_POLL_MS);
  private readonly session = inject(SessionService);
  protected readonly portfolios = inject(PortfolioContextService);

  private readonly jobs = signal<readonly ScheduledJobView[]>([]);
  private readonly now = signal(Date.now());

  protected readonly halted = computed(() => haltSummary(this.halts.active()));

  protected readonly next = computed(() => {
    const now = this.now();
    const job = nextJob(this.jobs(), now);
    if (!job?.next_run_at) return null;
    return {
      label: humanize(job.name),
      in: countdown(Date.parse(job.next_run_at) - now),
    };
  });

  /** Screen readers get a stable label; the ticking clock is hidden from them. */
  protected readonly nextLabel = computed(() => {
    const n = this.next();
    return n ? `Next scheduled run: ${n.label}. Open the schedule.` : null;
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
    try {
      this.jobs.set((await this.schedule.overview({ limit: 1 }, true)).jobs);
    } catch {
      // No scheduler or signed out: the strip just shows no next run.
    }
  }
}
