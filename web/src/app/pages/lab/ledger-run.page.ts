import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { LabService } from '../../api/lab.service';
import type { LedgerTrialView } from '../../api/models';
import { formatDateTime, formatNumber } from '../../core/format/format';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { HelpTip } from '../../shared/ui/help-tip';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { LabNav } from './lab-nav';
import { className, scoreText } from './ledger.page';

/** `{lookback_days: 20, threshold: 0}` -> "lookback_days 20, threshold 0". */
export function paramsText(params: Record<string, unknown>): string {
  const parts = Object.entries(params).map(
    ([k, v]) => `${k} ${typeof v === 'number' ? formatNumber(v) : JSON.stringify(v)}`,
  );
  return parts.length ? parts.join(', ') : 'No parameters';
}

/** One recorded lab run: what it set out to show, its data, and every trial. */
@Component({
  selector: 'app-ledger-run-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    LabNav,
    DataTable,
    TableCell,
    HelpTip,
    StatTile,
    StatusPill,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header title="Lab" description="One recorded lab run and every trial it tried." />
    <app-lab-nav />

    <a class="back" routerLink="/lab/ledger">Back to the trial ledger</a>

    @if (run.error(); as err) {
      <app-error-state title="Could not load this run" [error]="err" (retry)="run.reload()" />
    } @else if (!run.hasValue()) {
      <app-loading-state label="Loading the run" [rows]="6" />
    } @else {
      @let r = run.value();
      <section class="panel" aria-labelledby="run-title">
        <div class="panel-head">
          <h2 id="run-title">{{ strategyName() }}, {{ when(r.started_at) }}</h2>
          @if (r.verdict) {
            <app-status-pill [status]="r.verdict" />
          }
        </div>
        <div class="panel-body">
          <div class="tiles">
            <app-stat-tile label="Trials this run" help="trials" [value]="count(r.n_trials)" />
            <app-stat-tile label="Failed trials" [value]="count(r.n_failed)" />
            <app-stat-tile label="Best score" [value]="best()" [detail]="r.objective" />
            <app-stat-tile
              label="Trials of this strategy"
              help="trials"
              [value]="count(r.n_trials_class)"
              detail="Every run so far"
            />
          </div>
          <p class="lead">
            {{ strategyName() }} has had {{ count(r.n_trials_class) }} trials across every run. The
            more trials a strategy has had, the more likely a good result is luck, so the lab's
            checks, such as the deflated Sharpe <app-help-tip term="deflated_sharpe" />, count them
            all.
          </p>
          <dl class="facts">
            <div>
              <dt>Hypothesis</dt>
              <dd>{{ r.hypothesis || 'None written' }}</dd>
            </div>
            <div>
              <dt>Premortem</dt>
              <dd>{{ r.premortem || 'None written' }}</dd>
            </div>
            <div>
              <dt>Data</dt>
              <dd>{{ dataText() }}</dd>
            </div>
            <div>
              <dt>Search</dt>
              <dd>{{ searchText() }}</dd>
            </div>
          </dl>
        </div>
      </section>

      <section class="panel" aria-labelledby="trials-title">
        <div class="panel-head">
          <h2 id="trials-title">Trials</h2>
        </div>
        @if (r.trials.length === 0) {
          <app-empty-state
            title="No trials recorded"
            message="The run stopped before its first trial finished."
          />
        } @else {
          <app-data-table
            caption="Every trial of this run"
            [rows]="r.trials"
            [columns]="columns"
            [rowKey]="trialKey"
            [pageSize]="50"
            [initialSort]="{ key: 'trial_index', dir: 'asc' }"
          >
            <ng-template appCell="params" [appCellOf]="r.trials" let-t>
              <span class="params">{{ params(t.params) }}</span>
            </ng-template>
            <ng-template appCell="status" [appCellOf]="r.trials" let-t>
              <app-status-pill [status]="t.status === 'ok' ? 'passed' : 'failed'" />
            </ng-template>
          </app-data-table>
        }
      </section>
    }
  `,
  styleUrl: './ledger.page.scss',
})
export class LedgerRunPage {
  private readonly lab = inject(LabService);

  readonly runId = input.required<string>();

  protected readonly run = resource({
    params: () => ({ id: this.runId() }),
    loader: ({ params }) => this.lab.ledgerRun(params.id),
  });

  protected readonly strategyName = computed(() =>
    this.run.hasValue() ? className(this.run.value().strategy_class) : '',
  );
  protected readonly best = computed(() =>
    this.run.hasValue() ? scoreText(this.run.value().best_score) : '',
  );
  protected readonly dataText = computed(() => {
    if (!this.run.hasValue()) return '';
    const r = this.run.value();
    const basket = r.universe_id
      ? `the ${r.universe_id} universe (${r.tickers ?? 0} tickers)`
      : `${r.tickers ?? 0} tickers`;
    const window = r.start && r.end ? `, ${r.start} to ${r.end}` : '';
    return `${basket}${window}${r.interval ? `, ${r.interval} bars` : ''}`;
  });
  protected readonly searchText = computed(() => {
    if (!this.run.hasValue()) return '';
    const r = this.run.value();
    const parts = [r.tuner ? `${r.tuner} search` : null, r.budget ? `budget ${r.budget}` : null];
    if (r.seed != null) parts.push(`seed ${r.seed}`);
    return parts.filter(Boolean).join(', ') || 'Not recorded';
  });

  protected readonly trialKey = (t: LedgerTrialView) => String(t.trial_index);
  protected readonly when = (iso: string) => formatDateTime(iso);
  protected readonly count = (n: number | null | undefined) => formatNumber(n ?? 0);
  protected readonly params = paramsText;

  protected readonly columns: TableColumn<LedgerTrialView>[] = [
    { key: 'trial_index', label: 'Trial', format: 'number', mobile: 'title', help: false },
    { key: 'params', label: 'Parameters', sortable: false, value: (t) => paramsText(t.params) },
    { key: 'score', label: 'Score', value: (t) => t.score ?? null, format: 'number' },
    { key: 'n_bars', label: 'Bars', format: 'number', mobile: 'hide' },
    { key: 'status', label: 'Status' },
  ];
}
