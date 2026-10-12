import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { LabChoices } from '../../api/lab-choices.service';
import { LabService } from '../../api/lab.service';
import type { LedgerTrialView } from '../../api/models';
import { formatDateTime, formatNumber } from '../../core/format/format';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { HelpTip } from '../../shared/ui/help-tip';
import { ExportButton } from '../../shared/ui/export-button';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { robustnessWords } from '../../shared/lab-results/robustness';
import { StatusPill } from '../../shared/ui/status-pill';
import { SystemService } from '../../api/system.service';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { LabNav } from './lab-nav';
import { className, scoreText } from './ledger.page';
import { strategyTitle } from './lab-requests';
import { FAILED_RUN_TEXT } from '../../shared/lab-results/job-error';
import { JobFailure } from '../../shared/ui/job-failure';

/** `{lookback_days: 20, threshold: 0}` -> "Lookback days 20, threshold 0". */
export function paramsText(params: Record<string, unknown>): string {
  const parts = Object.entries(params).map(([k, v], i) => {
    const words = humanize(k);
    const name = i === 0 ? words : words.charAt(0).toLowerCase() + words.slice(1);
    return `${name} ${typeof v === 'number' ? formatNumber(v) : JSON.stringify(v)}`;
  });
  return parts.length ? parts.join(', ') : 'No settings';
}

const SEARCH_NAMES: Readonly<Record<string, string>> = {
  random: 'Random',
  grid: 'Grid',
  optuna: 'Bayesian',
};

/** The tuner a run used, in words: "random" -> "Random". */
export function searchName(tuner: string): string {
  return SEARCH_NAMES[tuner.toLowerCase().replace(/tuner$/, '')] ?? tuner;
}

/** One recorded lab run: what it set out to show, its data, and every trial. */
@Component({
  selector: 'app-ledger-run-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    ExportButton,
    LabNav,
    DataTable,
    TableCell,
    HelpTip,
    StatTile,
    StatusPill,
    JobFailure,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header title="Lab" description="One recorded lab run and every setting it tried.">
      <app-export-button
        actions
        kind="lab-trials"
        label="Settings CSV"
        [ghost]="true"
        [runId]="runId()"
      />
    </app-page-header>
    <app-lab-nav />

    <a class="back" routerLink="/lab/ledger">Back to the Lab history</a>

    @if (run.error(); as err) {
      <app-error-state title="Could not load this run" [error]="err" (retry)="run.reload()" />
    } @else if (!run.hasValue()) {
      <app-loading-state label="Loading the run" [rows]="6" />
    } @else {
      @let r = run.value();
      <section class="panel" aria-labelledby="run-title">
        <div class="panel-head">
          <h2 id="run-title">{{ strategyName() }}, {{ when(r.started_at) }}</h2>
          @let w = robustness(r.robustness);
          <span class="verdict">
            <span class="muted">Status</span>
            <app-status-pill [status]="w.status" [label]="w.label" />
          </span>
        </div>
        <div class="panel-body">
          @if (r.robustness === 'error') {
            <app-job-failure
              class="failed-reason callout"
              data-tone="loss"
              role="note"
              [live]="false"
              [text]="r.error || failedText"
              [detail]="r.error_detail"
            />
          }
          <div class="tiles summary-strip">
            <app-stat-tile label="Settings tried" [value]="count(r.n_trials)" detail="This run" />
            <app-stat-tile
              label="Settings with errors"
              [value]="count(r.n_failed)"
              detail="Settings that could not run"
            />
            <app-stat-tile label="Best score" [value]="best()" [detail]="objectiveName()" />
            <app-stat-tile
              label="Settings tried in all runs"
              [value]="count(r.n_trials_class)"
              detail="Of this strategy"
            />
          </div>
          <p class="lead">
            The more settings a strategy has tried, the more likely a good result is luck, so the
            robustness tests <app-help-tip term="robustness_tests" /> count them all.
          </p>
          <dl class="facts">
            @if (idea(); as idea) {
              <div>
                <dt>Idea</dt>
                <dd>{{ idea }}</dd>
              </div>
            }
            @if (r.premortem) {
              <div>
                <dt>Premortem</dt>
                <dd>{{ r.premortem }}</dd>
              </div>
            }
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
          <h2 id="trials-title">Settings tried</h2>
        </div>
        @if (r.trials.length === 0) {
          <app-empty-state
            icon="doc"
            title="No settings recorded"
            message="The run stopped before its first setting finished."
          />
        } @else {
          <app-data-table
            caption="Every setting this run tried"
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
              <app-status-pill
                [status]="t.status === 'ok' ? 'ok' : 'error'"
                [label]="t.status === 'ok' ? 'Done' : 'Error'"
              />
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
  private readonly system = inject(SystemService);
  private readonly choices = inject(LabChoices);

  readonly runId = input.required<string>();

  /** The catalog, for the strategy's plain name. */
  private readonly classes = resource({ loader: () => this.system.strategyClasses() });

  protected readonly robustness = robustnessWords;
  protected readonly run = resource({
    params: () => ({ id: this.runId() }),
    loader: ({ params }) => this.lab.ledgerRun(params.id),
  });

  protected readonly strategyName = computed(() => {
    if (!this.run.hasValue()) return '';
    const path = this.run.value().strategy_class;
    const known = this.classes.hasValue()
      ? this.classes.value().find((c) => c.class_path === path)
      : undefined;
    return known ? strategyTitle(known) : className(path);
  });
  protected readonly failedText = FAILED_RUN_TEXT;
  /** The idea the run tests: as written, else the catalog's description. */
  protected readonly idea = computed(() => {
    if (!this.run.hasValue()) return '';
    const r = this.run.value();
    if (r.hypothesis) return r.hypothesis;
    const known = this.classes.hasValue()
      ? this.classes.value().find((c) => c.class_path === r.strategy_class)
      : undefined;
    return known?.description ?? '';
  });
  protected readonly objectiveName = computed(() => {
    if (!this.run.hasValue()) return null;
    const o = this.run.value().objective;
    return o ? (this.choices.objectiveLabel(o) ?? o) : null;
  });
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
    const parts = [
      r.tuner ? `${searchName(r.tuner)} search` : null,
      r.budget ? `up to ${r.budget} settings` : null,
    ];
    return parts.filter(Boolean).join(', ') || 'Not recorded';
  });

  protected readonly trialKey = (t: LedgerTrialView) => String(t.trial_index);
  protected readonly when = (iso: string) => formatDateTime(iso);
  protected readonly count = (n: number | null | undefined) => formatNumber(n ?? 0);
  protected readonly params = paramsText;

  protected readonly columns: TableColumn<LedgerTrialView>[] = [
    { key: 'trial_index', label: 'No.', format: 'number', mobile: 'title', help: false },
    { key: 'params', label: 'Settings', sortable: false, value: (t) => paramsText(t.params) },
    {
      key: 'score',
      label: 'Score',
      value: (t) => t.score ?? null,
      display: (t) => scoreText(t.score),
      align: 'end',
    },
    { key: 'n_bars', label: 'Days of data', format: 'number', mobile: 'hide' },
    { key: 'status', label: 'Ran', help: false },
  ];
}
