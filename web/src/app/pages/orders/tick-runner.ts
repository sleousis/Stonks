import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  output,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { JobsApiService } from '../../api/jobs-api.service';
import type { BrokerInfo, TickResultView } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { TicksService } from '../../api/ticks.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate } from '../../core/format/format';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { strategyDisplayName } from '../../shared/strategy-names';
import { JobProgress, JobResult } from '../../shared/ui/job-progress';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PermissionNote } from '../../shared/ui/permission-note';
import { StatusPill } from '../../shared/ui/status-pill';
import {
  brokerLabel,
  isLiveBroker,
  realMoneyBooksLine,
  tickConfirmOptions,
  tickRequest,
  tickTicket,
} from './tick-confirm';
import { latestRealRunDay } from './tick-mode';

/**
 * Starts a trading run. Dry run is on by default and needs one click to
 * confirm; a real run shows an order ticket with the broker's PAPER or LIVE
 * stamp and needs the broker label typed, and cannot go behind the last real
 * run (the server refuses that, so Start waits). Both need
 * `operations.run` (admins). Progress follows the run's background job in
 * <app-job-progress>; the result links to the run, and a failed result read
 * offers Try again.
 */
@Component({
  selector: 'app-tick-runner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill, ModeStamp, PermissionNote, JobProgress],
  template: `
    <section class="panel runner" aria-labelledby="run-title" id="run">
      <div class="panel-head">
        <h2 id="run-title">Start a trading run</h2>
        @if (broker.hasValue()) {
          <span class="broker">
            {{ brokerText() }}
            <app-mode-stamp [live]="live()" />
          </span>
        }
      </div>

      <form class="panel-body form-grid" (submit)="$event.preventDefault(); run()">
        <label class="check">
          <input
            type="checkbox"
            name="dryRun"
            [checked]="dryRun()"
            (change)="dryRun.set($any($event.target).checked)"
          />
          Dry run
        </label>
        <p class="hint">
          @if (dryRun()) {
            Decides and sizes orders without sending them. Nothing reaches the broker.
          } @else {
            Sends orders to the broker and records the fills.
          }
        </p>

        @if (!dryRun()) {
          <div class="alert" role="note" [class.live]="live()">
            @if (broker.error()) {
              Could not read the broker. A run is blocked until it loads.
            } @else if (broker.hasValue()) {
              @if (live()) {
                Real-money run on the <strong>{{ brokerText() }}</strong> broker.
                {{ booksLine() }}
              } @else {
                Paper run on the <strong>{{ brokerText() }}</strong> broker. No real money moves.
              }
              You will be asked to type <strong>{{ brokerText() }}</strong> to confirm.
            } @else {
              Loading the broker…
            }
          </div>
        }

        <div class="form-grid form-grid-2 options">
          <div class="field">
            <label for="tick-as-of">As of</label>
            <input
              id="tick-as-of"
              class="input"
              type="date"
              [value]="asOf()"
              (change)="asOf.set($any($event.target).value)"
            />
            @if (backdated()) {
              <span class="hint warn" role="alert"
                >A real run cannot go behind the last one, on {{ lastRealText() }}. Pick that day or
                later, clear the date, or turn on dry run.</span
              >
            } @else {
              <span class="hint">Blank uses today. A real run cannot go behind the last one.</span>
            }
          </div>
          <div class="field">
            <label for="tick-tickers">Tickers</label>
            <input
              id="tick-tickers"
              class="input"
              placeholder="All in the universe"
              autocapitalize="characters"
              [value]="tickers()"
              (change)="tickers.set($any($event.target).value)"
            />
            <span class="hint">Optional, comma separated.</span>
          </div>
        </div>

        <div class="actions">
          <button
            type="submit"
            class="btn"
            [class.btn-primary]="dryRun() || !live()"
            [class.btn-danger]="!dryRun() && live()"
            [disabled]="!canRun()"
            [attr.aria-busy]="running()"
          >
            {{ running() ? 'Running…' : startLabel() }}
          </button>
          <app-permission-note permission="operations.run" />
        </div>
      </form>

      @if (job(); as h) {
        <app-job-progress label="Trading run" [handle]="h" [result]="resultRead">
          @if (h.status() === 'queued') {
            <button
              jobActions
              type="button"
              class="btn btn-ghost"
              [disabled]="cancelling()"
              (click)="cancel(h)"
            >
              {{ cancelling() ? 'Cancelling…' : 'Cancel' }}
            </button>
          }
        </app-job-progress>
      }

      @if (result(); as r) {
        <div class="result" aria-labelledby="result-title">
          <h3 id="result-title">
            {{ r.dry_run ? 'Dry-run result' : 'Result' }}
            <app-status-pill [status]="r.status" />
          </h3>
          <dl>
            <div>
              <dt>Orders</dt>
              <dd class="num">{{ r.orders_placed }}</dd>
            </div>
            <div>
              <dt>Fills</dt>
              <dd class="num">{{ r.fills }}</dd>
            </div>
            <div class="wide">
              <dt>Top strategy</dt>
              <dd>
                {{
                  r.winner_strategy_id
                    ? strategyName(r.winner_strategy_id, r.winner_strategy_name)
                    : 'None'
                }}
              </dd>
            </div>
          </dl>
          <a class="btn" [routerLink]="['/orders/ticks', r.tick_id]">Open this run</a>
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
    .broker {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .form-grid {
      max-width: none;
      gap: var(--space-3);
    }
    .options {
      gap: var(--space-3);
    }
    .hint {
      margin-top: calc(-1 * var(--space-2));
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .field .hint {
      margin-top: 0;
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .field .hint.warn {
      color: var(--color-loss);
    }
    .alert {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    /* Brass only where real money moves. */
    .alert.live {
      border-left-color: var(--color-live);
      background: var(--color-live-soft);
    }
    .actions {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
    }
    @include bp.phone {
      .actions .btn {
        width: 100%;
      }
    }
    .result {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border-top: 1px solid var(--color-border);
    }
    h3 {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-md);
    }
    dl {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-2);
      margin: 0;
    }
    dl .wide {
      grid-column: 1 / -1;
    }
    dt {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    dd {
      margin: 0;
      font-weight: var(--weight-medium);
      overflow-wrap: anywhere;
    }
    .result .btn {
      justify-self: start;
    }
  `,
})
export class TickRunner {
  private readonly ticksApi = inject(TicksService);
  private readonly jobsApi = inject(JobsApiService);
  private readonly system = inject(SystemService);
  private readonly confirm = inject(ConfirmService);
  private readonly jobs = inject(JobsService);
  private readonly toasts = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly session = inject(SessionService);

  /** Emits after a run ends, so the history can reload. */
  readonly finished = output<TickResultView | null>();

  protected readonly dryRun = signal(true);
  protected readonly asOf = signal('');
  protected readonly tickers = signal('');
  protected readonly starting = signal(false);
  protected readonly cancelling = signal(false);
  protected readonly job = signal<JobHandle | null>(null);
  /** The run's result, read after the job succeeds (a failed read offers Try again). */
  protected readonly resultRead = new JobResult<TickResultView>();
  protected readonly result = this.resultRead.value;
  /** POST /api/ticks needs operations.run, dry runs included. */
  protected readonly allowed = computed(() => this.session.can('operations.run'));

  protected readonly broker = resource({ loader: () => this.system.broker() });
  protected readonly booksLine = computed(() =>
    this.broker.hasValue() ? realMoneyBooksLine(this.broker.value()) : '',
  );
  protected readonly brokerText = computed(() =>
    this.broker.hasValue() ? brokerLabel(this.broker.value()) : '',
  );
  protected readonly live = computed(
    () => this.broker.hasValue() && isLiveBroker(this.broker.value()),
  );
  protected readonly startLabel = computed(() => {
    if (this.dryRun()) return 'Start dry run';
    return this.live() ? 'Start trading run' : 'Start paper run';
  });
  protected readonly strategyName = (id: string, name?: string | null) =>
    strategyDisplayName(id, { name });
  /** Recent runs, to know the last real run's date (admins only start runs). */
  private readonly recent = resource({
    params: () => (this.allowed() ? { limit: 20 } : undefined),
    loader: ({ params }) => this.ticksApi.list(params),
  });
  /** A real run started here moves the floor without a reload. */
  private readonly ranRealOn = signal<string | null>(null);
  protected readonly lastRealDay = computed(() => {
    const listed = this.recent.hasValue() ? latestRealRunDay(this.recent.value().items) : null;
    const here = this.ranRealOn();
    if (!listed) return here;
    if (!here) return listed;
    return here > listed ? here : listed;
  });
  protected readonly lastRealText = computed(() => formatDate(this.lastRealDay()));
  /** A real run dated before the last real run: the server refuses it, so Start waits. */
  protected readonly backdated = computed(() => {
    const floor = this.lastRealDay();
    return !this.dryRun() && !!this.asOf() && !!floor && this.asOf() < floor;
  });

  protected readonly running = computed(() => {
    const h = this.job();
    return this.starting() || (!!h && !h.done());
  });
  protected readonly canRun = computed(
    () =>
      this.allowed() &&
      !this.running() &&
      !this.backdated() &&
      (this.dryRun() || this.broker.hasValue()),
  );

  async run(): Promise<void> {
    if (!this.canRun()) return;
    const dryRun = this.dryRun();
    const broker: BrokerInfo | null = this.broker.hasValue() ? this.broker.value() : null;
    const ok = await this.confirm.confirm(
      dryRun || !broker
        ? tickConfirmOptions(dryRun, dryRun ? null : broker)
        : tickTicket(broker, { asOf: this.asOf(), tickers: this.tickers() }),
    );
    if (!ok) return;

    this.starting.set(true);
    this.resultRead.reset();
    try {
      const job = await this.ticksApi.start(
        tickRequest({ dryRun, asOf: this.asOf(), tickers: this.tickers() }),
      );
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.job.set(handle);
      this.starting.set(false);
      this.toasts.info(dryRun ? 'Started a dry run.' : 'Started a trading run.');

      const last = await handle.finished;
      this.ticksApi.announceFinished();
      let result: TickResultView | null = null;
      if (last?.status === 'succeeded') {
        result = await this.resultRead.load(() => this.ticksApi.result(job.id));
        if (result && !result.dry_run) {
          const day = this.asOf() || null;
          if (day) this.ranRealOn.set(day);
        }
        if (result) {
          this.toasts.success(
            `Ran the ${result.dry_run ? 'dry run' : 'trading run'}: ` +
              `${result.orders_placed} order${result.orders_placed === 1 ? '' : 's'}, ` +
              `${result.fills} fill${result.fills === 1 ? '' : 's'}.`,
          );
        }
      } else if (last?.status === 'cancelled') {
        this.toasts.info('Cancelled the trading run.');
      }
      this.finished.emit(result);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.starting.set(false);
    }
  }

  /** Cancel a run that has not started yet, after asking. */
  protected async cancel(h: JobHandle): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Cancel this trading run?',
      message: 'It has not started yet and will not run.',
      confirmLabel: 'Cancel run',
      cancelLabel: 'Keep it',
      tone: 'danger',
    });
    if (!ok) return;
    this.cancelling.set(true);
    try {
      await this.jobsApi.cancel(h.jobId);
    } catch {
      // Toasted by the error interceptor.
    } finally {
      this.cancelling.set(false);
    }
  }
}
