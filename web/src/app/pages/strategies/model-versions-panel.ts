import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import {
  type ModelVersionView,
  ModelVersionsService,
  type SwapReportView,
} from '../../api/model-versions.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { formatDateTime, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import {
  SWAP_OVERRIDE_MIN_REASON,
  compareBooks,
  eventWords,
  newestFirst,
  swapCheckRow,
  trainWindow,
  versionLook,
} from '../../shared/model-versions';
import { strategyDisplayName } from '../../shared/strategy-names';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PermissionNote } from '../../shared/ui/permission-note';
import { RetrainJob } from '../../shared/ui/retrain-job';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';

/**
 * The Versions tab of a strategy (roadmap 22.6, docs/model-lifecycle.md):
 * every fit of its model, the candidate's model book against the live
 * model over the same days, the swap check, and Swap in or Reject. A swap
 * asks for a fresh code, then a reason. A failing check needs an override
 * with a reason of at least 20 characters. Admins only (`strategy.promote`).
 */
@Component({
  selector: 'app-model-versions-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DataTable,
    TableCell,
    PermissionNote,
    RetrainJob,
    StatusChangeDialog,
    StatusPill,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <section class="panel" aria-labelledby="versions-title">
      <div class="panel-head">
        <h2 id="versions-title">Model versions</h2>
        @if (versions.hasValue()) {
          <span class="muted num">{{ versions.value().length }}</span>
        }
      </div>
      <p class="lead panel-body">
        A strategy that learns from data refits its model every week. Each new fit runs as a model
        book next to the live model. It trades only after someone swaps it in.
      </p>

      @if (versions.error(); as err) {
        <app-error-state
          title="Could not load the model versions"
          [error]="err"
          (retry)="versions.reload()"
        />
      } @else if (!versions.hasValue()) {
        <app-loading-state label="Loading model versions" [rows]="3" />
      } @else {
        @if (candidate(); as c) {
          <article class="candidate" aria-labelledby="candidate-title">
            <div class="candidate-head">
              <h3 id="candidate-title">Candidate v{{ c.version }}</h3>
              <app-status-pill
                [status]="c.status"
                [label]="look(c.status).label"
                [tone]="look(c.status).tone"
                [form]="look(c.status).form"
              />
            </div>
            <p class="muted">Trained on {{ window(c) }}. Fitted {{ dateTime(c.created_at) }}.</p>

            @if (check.error(); as err) {
              <app-error-state
                title="Could not load the swap check"
                [error]="err"
                (retry)="check.reload()"
              />
            } @else if (!check.hasValue()) {
              <app-loading-state label="Loading the swap check" [rows]="2" />
            } @else {
              @let r = check.value();
              @let b = comparison()!;
              <div class="books" role="group" aria-labelledby="books-title">
                <h4 id="books-title">Model book against the live model</h4>
                @if (b.days === 0) {
                  <p class="muted">
                    No model book days yet. Both books get a value after the next trading run.
                  </p>
                } @else {
                  <p class="muted">Over the same {{ b.days }} days, from the same cash.</p>
                }
                <dl class="bars">
                  <div class="bar-row">
                    <dt>Candidate v{{ r.version }}</dt>
                    <dd>
                      <span
                        class="bar cand"
                        [class.neg]="(b.candidate ?? 0) < 0"
                        [style.width.%]="b.candidateWidth"
                        aria-hidden="true"
                      ></span>
                      <span class="num">{{ pct(b.candidate, true) }}</span>
                    </dd>
                  </div>
                  <div class="bar-row">
                    <dt>Live v{{ r.live_version }}</dt>
                    <dd>
                      <span
                        class="bar live"
                        [class.neg]="(b.live ?? 0) < 0"
                        [style.width.%]="b.liveWidth"
                        aria-hidden="true"
                      ></span>
                      <span class="num">{{ pct(b.live, true) }}</span>
                    </dd>
                  </div>
                </dl>
                <dl class="facts">
                  <div>
                    <dt>Difference</dt>
                    <dd class="num" [class.gain]="(b.gap ?? 0) > 0" [class.loss]="(b.gap ?? 0) < 0">
                      {{ pct(b.gap, true) }}
                    </dd>
                  </div>
                  <div>
                    <dt>Candidate drawdown</dt>
                    <dd class="num">{{ pct(b.drawdown) }}</dd>
                  </div>
                  <div>
                    <dt>Days</dt>
                    <dd class="num">{{ b.days }}</dd>
                  </div>
                </dl>
              </div>

              <div class="swap-check" [class.failed]="!r.passed">
                <p class="check-head">
                  <app-status-pill
                    [status]="r.passed ? 'pass' : 'fail'"
                    [label]="r.passed ? 'Swap check passed' : 'Swap check failed'"
                  />
                  <span class="muted">
                    {{ passedCount() }} of {{ r.checks.length }} checks passed.
                  </span>
                </p>
                <ul class="checks" aria-label="Swap checks">
                  @for (row of checkRows(); track row.name) {
                    <li [class.failed]="!row.passed">
                      <app-status-pill [status]="row.passed ? 'pass' : 'fail'" />
                      <span class="check-label">{{ row.label }}</span>
                      <span class="num check-value">{{ row.value }}</span>
                      <span class="num muted">{{ row.limit }}</span>
                      <span class="check-detail">{{ row.detail }}</span>
                    </li>
                  }
                </ul>
              </div>

              <div class="actions">
                <button
                  type="button"
                  class="btn"
                  [disabled]="!canPromote() || busy() !== null"
                  [attr.aria-busy]="busy() === 'reject'"
                  (click)="reject(c)"
                >
                  {{ busy() === 'reject' ? 'Working…' : 'Reject' }}
                </button>
                <button
                  type="button"
                  class="btn btn-primary"
                  [disabled]="!canPromote() || busy() !== null"
                  [attr.aria-busy]="busy() === 'swap'"
                  (click)="swap(c, r)"
                >
                  {{
                    busy() === 'swap' ? 'Working…' : r.passed ? 'Swap in' : 'Override and swap in…'
                  }}
                </button>
              </div>
              @if (!canPromote()) {
                <app-permission-note permission="strategy.promote" />
              }
            }
          </article>
        }

        @if (versions.value().length === 0) {
          <app-empty-state
            title="No model versions"
            message="This strategy keeps no model of its own, so it never refits."
          />
        } @else {
          <app-data-table
            caption="Every fit of this strategy's model, oldest first"
            [rows]="versions.value()"
            [columns]="columns"
            [rowKey]="versionKey"
          >
            <ng-template appCell="version" [appCellOf]="versions.value()" let-v>
              <span class="num">v{{ v.version }}</span>
            </ng-template>
            <ng-template appCell="status" [appCellOf]="versions.value()" let-v>
              <app-status-pill
                [status]="v.status"
                [label]="look(v.status).label"
                [tone]="look(v.status).tone"
                [form]="look(v.status).form"
              />
            </ng-template>
          </app-data-table>
        }
      }

      <details class="retrain-box">
        <summary>Retrain this strategy now</summary>
        <p class="muted">
          Fits the model on the latest data. The new fit waits here as a candidate. Nothing trades
          until a swap.
        </p>
        <app-retrain-job [strategyId]="strategyId()" (finished)="reloadAll()" />
      </details>
    </section>

    <section class="panel" aria-labelledby="version-log-title">
      <div class="panel-head">
        <h2 id="version-log-title">Version log</h2>
      </div>
      @if (history.error(); as err) {
        <app-error-state
          title="Could not load the version log"
          [error]="err"
          (retry)="history.reload()"
        />
      } @else if (!history.hasValue()) {
        <app-loading-state label="Loading the version log" [rows]="3" />
      } @else if (log().length === 0) {
        <app-empty-state
          title="Nothing logged yet"
          message="Every fit, swap and rejection is kept here with who did it and why."
        />
      } @else {
        <ol class="timeline" aria-label="Version log, newest first">
          @for (e of log(); track e.id) {
            <li [class.override]="e.override">
              <p class="entry-head">
                <strong>v{{ e.version }}</strong>
                <span>{{ words(e.kind) }}</span>
                <time class="num muted" [attr.datetime]="e.created_at">{{
                  dateTime(e.created_at)
                }}</time>
              </p>
              @if (e.reason) {
                <p class="reason">{{ e.reason }}</p>
              }
              <p class="entry-meta">
                <span>By {{ e.actor }}</span>
                @if (e.check_passed !== null) {
                  <app-status-pill
                    [status]="e.check_passed ? 'pass' : 'fail'"
                    [label]="e.check_passed ? 'Check passed' : 'Check failed'"
                  />
                }
                @if (e.override) {
                  <app-status-pill status="warn" label="Check overridden" />
                }
              </p>
            </li>
          }
        </ol>
      }
    </section>

    <app-status-change-dialog />
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .lead,
    .muted {
      color: var(--color-ink-2);
    }
    .muted {
      font-size: var(--text-sm);
    }
    .candidate {
      display: grid;
      gap: var(--space-3);
      margin: 0 var(--space-4) var(--space-4);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border-strong);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      min-width: 0;

      @include bp.phone {
        margin: 0 var(--space-2) var(--space-3);
        padding: var(--space-3);
      }
    }
    .candidate-head,
    .check-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
    }
    .candidate-head h3,
    .books h4 {
      font-size: var(--text-md);
    }
    .books {
      display: grid;
      gap: var(--space-2);
    }
    .bars {
      display: grid;
      gap: var(--space-2);
      margin: 0;
    }
    .bar-row {
      display: grid;
      grid-template-columns: 8rem minmax(0, 1fr);
      align-items: center;
      gap: var(--space-2);

      @include bp.phone {
        grid-template-columns: minmax(0, 1fr);
        gap: var(--space-1);
      }
    }
    .bar-row dd {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      margin: 0;
      min-width: 0;
    }
    .bar {
      display: block;
      height: 0.75rem;
      min-width: 2px;
      max-width: calc(100% - 5rem);
      border-radius: var(--radius-sm);
      background: var(--color-info);
    }
    .bar.live {
      background: var(--color-ink-2);
    }
    .bar.neg {
      background: var(--color-loss);
    }
    .facts {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2) var(--space-5);
      margin: 0;
    }
    .facts dt {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .facts dd {
      margin: 0;
    }
    .gain {
      color: var(--color-gain);
    }
    .loss {
      color: var(--color-loss);
    }
    .swap-check {
      display: grid;
      gap: var(--space-2);
    }
    .checks {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .checks li {
      display: grid;
      grid-template-columns: auto minmax(8rem, 1fr) auto auto;
      align-items: baseline;
      gap: var(--space-1) var(--space-3);
      min-width: 0;

      @include bp.phone {
        grid-template-columns: auto minmax(0, 1fr);
      }
    }
    .check-detail {
      grid-column: 2 / -1;
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: var(--space-2);
      padding-top: var(--space-2);
      border-top: 1px dashed var(--color-border-strong);
    }
    .actions .btn {
      min-height: var(--touch-min);

      @include bp.phone {
        flex: 1 1 100%;
        justify-content: center;
      }
    }
    .retrain-box {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4) var(--space-4);
      border-top: 1px solid var(--color-border);
    }
    .retrain-box summary {
      display: flex;
      align-items: center;
      min-height: var(--touch-min);
      cursor: pointer;
      font-weight: var(--weight-medium);
    }
    .timeline {
      display: grid;
      gap: var(--space-3);
      margin: 0;
      padding: var(--space-3) var(--space-4) var(--space-4);
      list-style: none;
    }
    .timeline li {
      display: grid;
      gap: var(--space-1);
      padding-left: var(--space-3);
      border-left: 2px solid var(--color-border-strong);
      overflow-wrap: anywhere;
    }
    .timeline li.override {
      border-left-color: var(--color-warn);
    }
    .entry-head,
    .entry-meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-3);
    }
    .entry-meta {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
  `,
})
export class ModelVersionsPanel {
  private readonly api = inject(ModelVersionsService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly toasts = inject(ToastService);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  readonly strategyId = input.required<string>();

  protected readonly versions = resource({
    params: () => ({ id: this.strategyId() }),
    loader: ({ params }) => this.api.list(params.id),
  });
  protected readonly history = resource({
    params: () => ({ id: this.strategyId() }),
    loader: ({ params }) => this.api.history(params.id),
  });
  protected readonly log = computed(() =>
    this.history.hasValue() ? newestFirst(this.history.value()) : [],
  );

  /** The newest candidate: a retrain replaces an older one, so there is at most one. */
  protected readonly candidate = computed<ModelVersionView | null>(() => {
    if (!this.versions.hasValue()) return null;
    const found = this.versions.value().filter((v) => v.status === 'candidate');
    return found.length ? found[found.length - 1] : null;
  });
  protected readonly check = resource({
    params: () => {
      const c = this.candidate();
      return c ? { id: c.strategy_id, version: c.version } : undefined;
    },
    loader: ({ params }) => this.api.check(params.id, params.version),
  });
  protected readonly comparison = computed(() =>
    this.check.hasValue() ? compareBooks(this.check.value()) : null,
  );
  protected readonly checkRows = computed(() =>
    this.check.hasValue() ? this.check.value().checks.map(swapCheckRow) : [],
  );
  protected readonly passedCount = computed(() => this.checkRows().filter((c) => c.passed).length);

  protected readonly canPromote = computed(() => this.session.can('strategy.promote'));
  protected readonly busy = signal<'swap' | 'reject' | null>(null);

  protected readonly columns: readonly TableColumn<ModelVersionView>[] = [
    { key: 'version', label: 'Version', mobile: 'title', value: (v) => v.version },
    { key: 'status', label: 'Status', value: (v) => versionLook(v.status).label },
    { key: 'train', label: 'Trained on', value: (v) => trainWindow(v), sortable: false },
    { key: 'created_at', label: 'Fitted', format: 'date' },
    { key: 'created_by', label: 'By' },
  ];
  protected readonly versionKey = (v: ModelVersionView) => `v${v.version}`;
  protected readonly look = versionLook;
  protected readonly window = trainWindow;
  protected readonly words = eventWords;
  protected readonly dateTime = formatDateTime;
  protected pct(v: number | null, signed = false): string {
    return formatPercent(v, { signed });
  }

  protected reloadAll(): void {
    this.versions.reload();
    this.history.reload();
  }

  protected async swap(c: ModelVersionView, report: SwapReportView): Promise<void> {
    if (this.busy() || !this.canPromote()) return;
    const name = strategyDisplayName(c.strategy_id);
    const passed = report.passed;
    const body = await this.dialog().open({
      title: passed
        ? `Swap in model v${c.version} of ${name}?`
        : `Override the swap check for v${c.version}?`,
      message: passed
        ? 'The next trading run trades the new model. The live model is archived.'
        : 'The swap check did not pass. The override and your reason go into the version log. The next trading run trades the new model.',
      confirmLabel: passed ? 'Swap in' : 'Override and swap in',
      minReason: passed ? 1 : SWAP_OVERRIDE_MIN_REASON,
      override: !passed,
      reasonHint: 'Kept in the version log so others can see why.',
      ticket: {
        kind: 'Model swap',
        live: null,
        lines: [
          { label: 'Strategy', value: name },
          { label: 'From', value: `Live v${report.live_version}` },
          { label: 'To', value: `Candidate v${c.version}` },
          { label: 'Trained on', value: trainWindow(c) },
          { label: 'Model book days', value: String(report.days) },
        ],
      },
    });
    if (!body) return;
    if (!(await this.stepUp.ensure(`Swap in model v${c.version} of ${name}.`))) return;
    this.busy.set('swap');
    try {
      await this.api.swap(c.strategy_id, c.version, {
        reason: body.reason,
        override: !!body.override,
      });
      this.toasts.success(`Model v${c.version} of ${name} is live from the next trading run.`);
      this.reloadAll();
    } catch {
      // The error interceptor showed the API's message (a failing check, a retired strategy).
      this.check.reload();
    } finally {
      this.busy.set(null);
    }
  }

  protected async reject(c: ModelVersionView): Promise<void> {
    if (this.busy() || !this.canPromote()) return;
    const name = strategyDisplayName(c.strategy_id);
    const body = await this.dialog().open({
      title: `Reject model v${c.version} of ${name}?`,
      message: 'Its model book stops. The live model keeps trading.',
      confirmLabel: 'Reject',
      tone: 'danger',
      minReason: 1,
      reasonHint: 'Kept in the version log so others can see why.',
    });
    if (!body) return;
    this.busy.set('reject');
    try {
      await this.api.reject(c.strategy_id, c.version, body.reason ?? '');
      this.toasts.success(`Model v${c.version} of ${name} is rejected.`);
      this.reloadAll();
    } catch {
      // The error interceptor showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
