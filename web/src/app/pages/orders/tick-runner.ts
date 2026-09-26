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

import type { BrokerInfo, TickResultView } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { TicksService } from '../../api/ticks.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { StatusPill } from '../../shared/ui/status-pill';
import { brokerLabel, isLiveBroker, tickConfirmOptions, tickRequest } from './tick-confirm';

/**
 * Starts a production tick. Dry run is on by default and needs one click to
 * confirm; a real tick shows the broker first and needs its label typed.
 * Progress follows the tick's background job; the result links to the tick.
 */
@Component({
  selector: 'app-tick-runner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatusPill],
  template: `
    <section class="panel runner" aria-labelledby="run-title" id="run">
      <div class="panel-head">
        <h2 id="run-title">Run tick</h2>
        @if (broker.hasValue()) {
          <span class="broker">
            Broker
            <app-status-pill
              [status]="live() ? 'warning' : 'info'"
              [tone]="live() ? 'warn' : 'info'"
              [label]="brokerText()"
            />
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
            Sends orders to the broker and records fills in the ledger.
          }
        </p>

        @if (!dryRun()) {
          <div class="alert" role="note">
            @if (broker.error()) {
              Could not read the broker configuration. A real tick is blocked until it loads.
            } @else if (broker.hasValue()) {
              Real tick on the <strong>{{ brokerText() }}</strong> broker.
              @if (live()) {
                This account trades real money.
              }
              You will be asked to type <strong>{{ brokerText() }}</strong> to confirm.
            } @else {
              Loading the broker configuration…
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
            <span class="hint">Blank uses today. Real ticks cannot be backdated.</span>
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
            {{ running() ? 'Running…' : dryRun() ? 'Run dry run' : 'Run tick' }}
          </button>
        </div>
      </form>

      @if (job(); as h) {
        <div class="progress-block" aria-live="polite">
          <div class="progress-head">
            <app-status-pill [status]="h.status() ?? 'queued'" />
            <span class="muted">{{ h.message() ?? 'Waiting for the worker' }}</span>
          </div>
          @if (!h.done()) {
            <progress [value]="h.progress()" max="1" aria-label="Tick progress"></progress>
          }
          @if (h.error(); as err) {
            <p class="alert error" role="alert">{{ err }}</p>
          }
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
          <a class="btn" [routerLink]="['/orders/ticks', r.tick_id]">Open tick {{ r.tick_id }}</a>
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
      justify-content: flex-end;
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

  /** Emits after a tick ends, so the history can reload. */
  readonly finished = output<TickResultView | null>();

  protected readonly dryRun = signal(true);
  protected readonly asOf = signal('');
  protected readonly tickers = signal('');
  protected readonly starting = signal(false);
  protected readonly job = signal<JobHandle | null>(null);
  protected readonly result = signal<TickResultView | null>(null);

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
    () => !this.running() && (this.dryRun() || this.broker.hasValue()),
  );

  async run(): Promise<void> {
    if (!this.canRun()) return;
    const dryRun = this.dryRun();
    const broker: BrokerInfo | null = this.broker.hasValue() ? this.broker.value() : null;
    const ok = await this.confirm.confirm(tickConfirmOptions(dryRun, dryRun ? null : broker));
    if (!ok) return;

    this.starting.set(true);
    this.result.set(null);
    try {
      const job = await this.ticksApi.start(
        tickRequest({ dryRun, asOf: this.asOf(), tickers: this.tickers() }),
      );
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.job.set(handle);
      this.starting.set(false);
      this.toasts.info(dryRun ? 'Started a dry-run tick.' : 'Started a tick.');

      const last = await handle.finished;
      let result: TickResultView | null = null;
      if (last?.status === 'succeeded') {
        result = await this.ticksApi.result(job.id);
        this.result.set(result);
        this.toasts.success(
          `Ran ${result.dry_run ? 'dry-run ' : ''}tick ${result.tick_id}: ` +
            `${result.orders_placed} order${result.orders_placed === 1 ? '' : 's'}, ` +
            `${result.fills} fill${result.fills === 1 ? '' : 's'}.`,
        );
      }
      this.finished.emit(result);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.starting.set(false);
    }
  }
}
