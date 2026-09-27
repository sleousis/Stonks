import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';

import type {
  OptionStrategyView,
  OptionUnderlyingView,
  OptionsBacktestView,
} from '../../api/models';
import { OptionsService } from '../../api/options.service';
import { SessionService } from '../../core/auth/session.service';
import { formatNumber, formatPercent } from '../../core/format/format';
import { JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { JobProgress } from '../../shared/ui/job-progress';
import { ParamForm } from '../../shared/ui/param-form/param-form';
import {
  type ParamValues,
  defaultParamValues,
  paramErrors,
  paramPayload,
} from '../../shared/ui/param-form/param-spec';
import { PermissionNote } from '../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { JobFollower } from '../lab/job-follower';
import {
  type BacktestForm,
  backtestErrors,
  buildBacktestRequest,
  checkLabel,
  defaultBacktestForm,
} from './options-view';

/**
 * Runs an options backtest on the stored chains as a background job, with
 * its validation checks, and shows the equity curve, the figures and the
 * verdict. Research only: nothing it does trades options.
 */
@Component({
  selector: 'app-options-backtest',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ParamForm,
    JobProgress,
    PermissionNote,
    StatusPill,
    TimeSeriesChart,
    ErrorState,
    LoadingState,
  ],
  template: `
    @let f = form();
    <form class="form" novalidate (submit)="$event.preventDefault(); start()">
      <div class="grid">
        <div class="field">
          <label for="obt-strategy">Strategy</label>
          <select
            id="obt-strategy"
            class="input"
            [attr.aria-invalid]="!!errors()['strategy']"
            (change)="pickStrategy($any($event.target).value)"
          >
            <option value="" [selected]="!f.strategy">Pick a strategy</option>
            @for (s of strategies(); track s.id) {
              <option [value]="s.id" [selected]="f.strategy === s.id">{{ s.id }}</option>
            }
          </select>
          @if (errors()['strategy']; as e) {
            <span class="error">{{ e }}</span>
          }
        </div>
        <div class="field">
          <label for="obt-underlyings">Underlyings</label>
          <input
            id="obt-underlyings"
            class="input"
            autocomplete="off"
            aria-describedby="obt-underlyings-hint"
            [value]="f.underlyings"
            [attr.aria-invalid]="!!errors()['underlyings']"
            (input)="patch({ underlyings: $any($event.target).value })"
          />
          @if (errors()['underlyings']; as e) {
            <span class="error" id="obt-underlyings-hint">{{ e }}</span>
          } @else {
            <span class="hint" id="obt-underlyings-hint">Separate with commas.</span>
          }
        </div>
        <div class="field">
          <label for="obt-start">From</label>
          <input
            id="obt-start"
            class="input"
            type="date"
            [value]="f.start"
            [attr.aria-invalid]="!!errors()['start']"
            (input)="patch({ start: $any($event.target).value })"
          />
          @if (errors()['start']; as e) {
            <span class="error">{{ e }}</span>
          }
        </div>
        <div class="field">
          <label for="obt-end">To</label>
          <input
            id="obt-end"
            class="input"
            type="date"
            [value]="f.end"
            [attr.aria-invalid]="!!errors()['end']"
            (input)="patch({ end: $any($event.target).value })"
          />
          @if (errors()['end']; as e) {
            <span class="error">{{ e }}</span>
          }
        </div>
        <div class="field">
          <label for="obt-cash">Starting cash</label>
          <input
            id="obt-cash"
            class="input num"
            type="number"
            inputmode="decimal"
            min="1"
            [value]="f.cash ?? ''"
            [attr.aria-invalid]="!!errors()['cash']"
            (input)="setCash($any($event.target).value)"
          />
          @if (errors()['cash']; as e) {
            <span class="error">{{ e }}</span>
          }
        </div>
        <label class="check">
          <input
            type="checkbox"
            [checked]="f.validation"
            (change)="patch({ validation: $any($event.target).checked })"
          />
          Run the validation checks
        </label>
      </div>

      @if (strategy(); as s) {
        <p class="hypothesis">{{ s.hypothesis }}</p>
        <app-param-form
          idPrefix="obt-param"
          [params]="s.parameters"
          [values]="params()"
          (valuesChange)="params.set($event)"
          [showErrors]="tried()"
        />
      }

      <div class="actions">
        <button class="btn btn-primary" type="submit" [disabled]="starting() || !canRun()">
          {{ starting() ? 'Starting' : 'Run backtest' }}
        </button>
        @if (!canRun()) {
          <app-permission-note permission="lab.run" />
        }
      </div>
    </form>

    @if (run.handle(); as h) {
      @if (!h.done() || h.status() !== 'succeeded') {
        <app-job-progress label="Options backtest" [handle]="h" />
      }
      @if (run.error(); as err) {
        <app-error-state title="Could not load the result" [error]="err" (retry)="run.retry()" />
      } @else if (run.loading()) {
        <app-loading-state label="Loading the result" [rows]="4" />
      } @else if (run.result(); as r) {
        <section class="result" aria-labelledby="obt-result-title">
          <div class="result-head">
            <h3 id="obt-result-title">{{ r.strategy }} on {{ r.underlyings.join(', ') }}</h3>
            @if (r.verdict !== 'not_run') {
              <app-status-pill
                [status]="r.verdict"
                [label]="r.verdict === 'passed' ? 'Checks passed' : 'Checks failed'"
              />
            }
          </div>
          @if (r.synthetic) {
            <p class="warn" role="note">
              These chains are generated, not market quotes. The result shows the tools work and is
              never evidence for the strategy.
            </p>
          }
          <dl class="figures">
            <div>
              <dt>Return</dt>
              <dd>{{ pct(r.final_return) }}</dd>
            </div>
            <div>
              <dt>Sharpe</dt>
              <dd>{{ num(r.sharpe) }}</dd>
            </div>
            <div>
              <dt>Max drawdown</dt>
              <dd>{{ pct(r.max_drawdown) }}</dd>
            </div>
            <div>
              <dt>Fills</dt>
              <dd>{{ r.fills }}</dd>
            </div>
            <div>
              <dt>Not filled</dt>
              <dd>{{ r.rejected }}</dd>
            </div>
            <div>
              <dt>Days</dt>
              <dd>{{ r.days }}</dd>
            </div>
          </dl>
          @if (series(r).length) {
            <app-time-series-chart
              ariaLabel="Options backtest equity"
              [summary]="chartSummary(r)"
              [series]="series(r)"
              [height]="240"
            />
          }
          @if (r.validation.length) {
            <h4>Validation checks</h4>
            <ul class="checks">
              @for (c of r.validation; track c.test_id) {
                <li>
                  <span>{{ label(c.test_id) }}</span>
                  <app-status-pill [status]="c.passed ? 'passed' : 'failed'" />
                </li>
              }
            </ul>
          }
        </section>
      }
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .form {
      display: grid;
      gap: var(--space-3);
    }
    .grid {
      display: grid;
      gap: var(--space-3);
      @include bp.from-tablet {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .check {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      align-self: end;
    }
    .hypothesis {
      margin: 0;
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-3);
    }
    .result {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .result-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    h3,
    h4 {
      margin: 0;
      font-size: var(--text-md);
      overflow-wrap: anywhere;
    }
    .warn {
      margin: 0;
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
    }
    .figures {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin: 0;
      @include bp.from-tablet {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
      dt {
        color: var(--color-ink-2);
        font-size: var(--text-sm);
      }
      dd {
        margin: 0;
        font-family: var(--font-mono);
        font-size: var(--text-lg);
      }
    }
    .checks {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
      li {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--space-2);
        min-height: var(--touch-min);
      }
    }
  `,
})
export class OptionsBacktest {
  private readonly api = inject(OptionsService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  readonly strategies = input<readonly OptionStrategyView[]>([]);
  readonly underlyings = input<readonly OptionUnderlyingView[]>([]);

  protected readonly canRun = computed(() => this.session.can('lab.run'));
  protected readonly form = signal<BacktestForm>(defaultBacktestForm());
  protected readonly params = signal<ParamValues>({});
  protected readonly tried = signal(false);
  protected readonly starting = signal(false);

  protected readonly strategy = computed(
    () => this.strategies().find((s) => s.id === this.form().strategy) ?? null,
  );
  private readonly allErrors = computed(() => {
    const s = this.strategy();
    const errs = backtestErrors(this.form());
    if (s && Object.keys(paramErrors(s.parameters, this.params())).length) {
      errs['params'] = 'Fix the parameters.';
    }
    return errs;
  });
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));

  protected readonly run = new JobFollower<OptionsBacktestView>(
    inject(JobsService),
    inject(DestroyRef),
    (id) => this.api.backtestResult(id),
  );

  constructor() {
    // Fill the window from the first stored underlying once they load.
    effect(() => {
      const first = this.underlyings()[0];
      const f = this.form();
      if (first && !f.underlyings && !f.start && !f.end) {
        this.form.set({
          ...f,
          underlyings: first.underlying,
          start: first.first_day,
          end: first.last_day,
        });
      }
    });
  }

  protected patch(p: Partial<BacktestForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setCash(raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ cash: n === null || Number.isNaN(n) ? null : n });
  }

  protected pickStrategy(id: string): void {
    this.patch({ strategy: id });
    const s = this.strategies().find((x) => x.id === id);
    this.params.set(s ? defaultParamValues(s.parameters) : {});
  }

  protected pct(v: number | null | undefined): string {
    return v === null || v === undefined ? 'n/a' : formatPercent(v);
  }

  protected num(v: number | null | undefined): string {
    return v === null || v === undefined ? 'n/a' : formatNumber(v, { digits: 2 });
  }

  protected label(id: string): string {
    return checkLabel(id);
  }

  protected series(r: OptionsBacktestView): ChartSeries[] {
    if (!r.equity.length) return [];
    return [
      {
        id: 'equity',
        label: 'Equity',
        kind: 'line',
        color: 'primary',
        format: 'money',
        points: r.equity.map((p) => ({ time: p.date, value: p.value })),
      },
    ];
  }

  protected chartSummary(r: OptionsBacktestView): string {
    return `Equity of ${r.strategy} over ${r.days} days, ending at a return of ${this.pct(r.final_return)}.`;
  }

  async start(): Promise<void> {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    const s = this.strategy();
    const body = buildBacktestRequest(
      this.form(),
      s ? paramPayload(s.parameters, this.params()) : {},
    );
    this.starting.set(true);
    let jobId: string;
    try {
      jobId = (await this.api.startBacktest(body)).id;
    } catch {
      return; // toasted by the error interceptor
    } finally {
      this.starting.set(false);
    }
    this.toasts.success(`Started the options backtest of ${body.strategy}.`);
    await this.run.follow(jobId);
  }
}
