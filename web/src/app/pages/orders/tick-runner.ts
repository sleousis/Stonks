import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  output,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { BrokerInfo, TickResultView } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { TicksService } from '../../api/ticks.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PermissionNote } from '../../shared/ui/permission-note';
import { ErrorState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import {
  brokerLabel,
  isLiveBroker,
  tickConfirmOptions,
  tickRequest,
  tickTicket,
} from './tick-confirm';
import { TickTicketDialog } from './tick-ticket-dialog';

/**
 * Starts a trading run (a production tick). Dry run is on by default and
 * needs one click to confirm; a real run shows an order ticket with the
 * broker's PAPER or LIVE stamp and needs the broker label typed. Both need
 * `operations.run` (admins). Progress follows the run's background job; the
 * result links to the run, and a failed result load offers Retry.
 */
@Component({
  selector: 'app-tick-runner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill, ModeStamp, PermissionNote, ErrorState, TickTicketDialog],
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
          <div class="alert" role="note">
            @if (broker.error()) {
              Could not read the broker. A real run is blocked until it loads.
            } @else if (broker.hasValue()) {
              Real run on the <strong>{{ brokerText() }}</strong> broker.
              @if (live()) {
                This account trades real money.
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
            <span class="hint">Blank uses today. Real runs cannot be backdated.</span>
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
            [class.btn-primary]="dryRun()"
            [class.btn-danger]="!dryRun()"
            [disabled]="!canRun()"
            [attr.aria-busy]="running()"
          >
            {{ running() ? 'Running…' : dryRun() ? 'Start dry run' : 'Start trading run' }}
          </button>
          <app-permission-note permission="operations.run" />
        </div>
      </form>

      @if (job(); as h) {
        <div class="progress-block" aria-live="polite">
          <div class="progress-head">
            <app-status-pill [status]="h.status() ?? 'queued'" />
            <span class="muted">{{ h.message() ?? 'Waiting for the worker' }}</span>
          </div>
          @if (!h.done()) {
            <progress [value]="h.progress()" max="1" aria-label="Trading run progress"></progress>
          }
          @if (h.error(); as err) {
            <p class="alert error" role="alert">{{ err }}</p>
          }
        </div>
      }

      @if (resultError(); as err) {
        <div class="result">
          <app-error-state
            title="The run finished, but its result could not load"
            [error]="err"
            (retry)="reloadResult()"
          />
        </div>
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
              <dt>Winner</dt>
              <dd>{{ r.winner_strategy_id ?? 'None' }}</dd>
            </div>
          </dl>
          <a class="btn" [routerLink]="['/orders/ticks', r.tick_id]">Open this run</a>
        </div>
      }
    </section>
    <app-tick-ticket-dialog />
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
    .alert {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .alert.error {
      border-left-color: var(--color-loss);
      background: var(--color-loss-soft);
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
    .progress-block,
    .result {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border-top: 1px solid var(--color-border);
    }
    .progress-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-sm);
    }
    progress {
      width: 100%;
      height: 6px;
      accent-color: var(--color-brass);
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
  private readonly system = inject(SystemService);
  private readonly confirm = inject(ConfirmService);
  private readonly jobs = inject(JobsService);
  private readonly toasts = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly session = inject(SessionService);
  private readonly ticket = viewChild.required(TickTicketDialog);

  /** Emits after a run ends, so the history can reload. */
  readonly finished = output<TickResultView | null>();

  protected readonly dryRun = signal(true);
  protected readonly asOf = signal('');
  protected readonly tickers = signal('');
  protected readonly starting = signal(false);
  protected readonly job = signal<JobHandle | null>(null);
  protected readonly result = signal<TickResultView | null>(null);
  /** The job succeeded but GET result failed (GETs are not toasted). */
  protected readonly resultError = signal<unknown>(null);
  private resultJobId: string | null = null;
  /** POST /api/ticks needs operations.run, dry runs included. */
  protected readonly allowed = computed(() => this.session.can('operations.run'));

  protected readonly broker = resource({ loader: () => this.system.broker() });
  protected readonly brokerText = computed(() =>
    this.broker.hasValue() ? brokerLabel(this.broker.value()) : '',
  );
  protected readonly live = computed(
    () => this.broker.hasValue() && isLiveBroker(this.broker.value()),
  );

  protected readonly running = computed(() => {
    const h = this.job();
    return this.starting() || (!!h && !h.done());
  });
  protected readonly canRun = computed(
    () => this.allowed() && !this.running() && (this.dryRun() || this.broker.hasValue()),
  );

  async run(): Promise<void> {
    if (!this.canRun()) return;
    const dryRun = this.dryRun();
    const broker: BrokerInfo | null = this.broker.hasValue() ? this.broker.value() : null;
    const ok =
      dryRun || !broker
        ? await this.confirm.confirm(tickConfirmOptions(dryRun, dryRun ? null : broker))
        : await this.ticket().open(
            tickTicket(broker, { asOf: this.asOf(), tickers: this.tickers() }),
          );
    if (!ok) return;

    this.starting.set(true);
    this.result.set(null);
    this.resultError.set(null);
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
      if (last?.status === 'succeeded') result = await this.loadResult(job.id);
      this.finished.emit(result);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.starting.set(false);
    }
  }

  /** Retry after the result GET failed. */
  protected async reloadResult(): Promise<void> {
    if (this.resultJobId) await this.loadResult(this.resultJobId);
  }

  private async loadResult(jobId: string): Promise<TickResultView | null> {
    this.resultJobId = jobId;
    this.resultError.set(null);
    try {
      const result = await this.ticksApi.result(jobId);
      this.result.set(result);
      this.toasts.success(
        `Ran the ${result.dry_run ? 'dry run' : 'trading run'}: ` +
          `${result.orders_placed} order${result.orders_placed === 1 ? '' : 's'}, ` +
          `${result.fills} fill${result.fills === 1 ? '' : 's'}.`,
      );
      return result;
    } catch (err) {
      // GET failures are not toasted, so say it here with a Retry.
      this.resultError.set(err);
      return null;
    }
  }
}
