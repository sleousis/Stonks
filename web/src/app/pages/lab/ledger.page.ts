import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { LabService } from '../../api/lab.service';
import type { LedgerRunView } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { formatDateTime, formatNumber } from '../../core/format/format';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { ExportButton } from '../../shared/ui/export-button';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { robustnessWords } from '../../shared/lab-results/robustness';
import { StatusPill } from '../../shared/ui/status-pill';
import { ColumnsPicker, visibleColumns } from '../../shared/ui/columns-picker';
import { LabNav } from './lab-nav';
import { strategyTitle } from './lab-requests';

const PAGE_SIZE = 25;

/** "stonks.strategies.examples.momentum:Momentum" -> "Momentum". */
export function className(classPath: string | null | undefined): string {
  if (!classPath) return '';
  return classPath.split(':').at(-1) ?? classPath;
}

/** A score for a table cell: 2 decimals, or n/a when the run has none. */
export function scoreText(score: number | null | undefined): string {
  return score == null || !Number.isFinite(score) ? 'n/a' : formatNumber(score, { digits: 2 });
}

/**
 * The Lab history: every lab run anyone recorded, newest first, with what
 * it set out to show and how many settings it tried. Filter by strategy,
 * open a run for the settings it tried.
 */
@Component({
  selector: 'app-ledger-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    ExportButton,
    LabNav,
    ColumnsPicker,
    DataTable,
    TableCell,
    StatusPill,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header title="Lab" [description]="standfirst()">
      <app-export-button actions kind="lab-trials" label="All settings CSV" [ghost]="true" />
    </app-page-header>
    <app-lab-nav />

    <section class="page-section" aria-labelledby="ledger-title">
      <header class="section-head">
        <h2 id="ledger-title">
          Lab history
          @if (page(); as p) {
            <span class="count">{{ p.total }} runs</span>
          }
        </h2>
      </header>
      <div class="section-body">
        <p class="lead callout">
          Each lab run is written down before it starts, with the idea it tests, then every setting
          it tries. The more settings a strategy has tried, the more likely a good result is luck,
          so the robustness tests count them all.
        </p>
        <div class="toolbar">
          <form
            class="filters"
            role="search"
            aria-label="Filter the Lab history"
            (submit)="$event.preventDefault()"
          >
            <div class="field">
              <label for="ledger-class">Strategy</label>
              <select
                id="ledger-class"
                class="input"
                (change)="setClass($any($event.target).value)"
              >
                <option value="" [selected]="!strategy()">All strategies</option>
                @for (c of classOptions(); track c.class_path) {
                  <option [value]="c.class_path" [selected]="c.class_path === strategy()">
                    {{ title(c) }}
                  </option>
                }
              </select>
            </div>
          </form>
          <app-columns-picker
            [columns]="columns"
            storageKey="stonks.lab.ledger.columns"
            [defaultHidden]="['n_failed']"
            (hiddenChange)="hidden.set($event)"
          />
        </div>

        @if (runs.error(); as err) {
          <app-error-state
            title="Could not load the Lab history"
            [error]="err"
            (retry)="runs.reload()"
          />
        } @else if (page(); as p) {
          @if (p.items.length === 0) {
            <app-empty-state
              [title]="strategy() ? 'No runs of this strategy yet' : 'No lab runs yet'"
              message="Every lab run and sweep is recorded here with the settings it tries as soon as it starts."
            >
              <a class="btn" routerLink="/lab">Test a strategy</a>
            </app-empty-state>
          } @else {
            @for (k of [strategy()]; track k) {
              <app-data-table
                caption="Recorded lab runs"
                [rows]="p.items"
                [columns]="shownColumns()"
                [rowKey]="key"
                [total]="p.total"
                [offset]="p.offset"
                [busy]="runs.isLoading()"
                [pageSize]="pageSize"
                (pageChange)="offset.set($event.offset)"
              >
                <ng-template appCell="started_at" [appCellOf]="p.items" let-r>
                  <a class="cell-link num" [routerLink]="['/lab/ledger', r.id]">{{
                    when(r.started_at)
                  }}</a>
                </ng-template>
                <ng-template appCell="robustness" [appCellOf]="p.items" let-r>
                  @let w = robustness(r.robustness);
                  <app-status-pill [status]="w.status" [label]="w.label" />
                </ng-template>
              </app-data-table>
            }
          }
        } @else {
          <app-loading-state label="Loading the Lab history" [rows]="6" />
        }
      </div>
    </section>
  `,
  styleUrl: './ledger.page.scss',
})
export class LedgerPage {
  private readonly lab = inject(LabService);
  private readonly system = inject(SystemService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /** `?strategy=module:Class` narrows the list. */
  readonly strategy = input<string>();

  protected readonly pageSize = PAGE_SIZE;
  protected readonly key = (r: LedgerRunView) => r.id;
  protected readonly robustness = robustnessWords;
  protected readonly when = (iso: string) => formatDateTime(iso);

  private readonly classes = resource({ loader: () => this.system.strategyClasses() });
  protected readonly classOptions = computed(() =>
    this.classes.hasValue()
      ? [...this.classes.value()].sort((a, b) => strategyTitle(a).localeCompare(strategyTitle(b)))
      : [],
  );
  protected readonly title = strategyTitle;
  /** Class path to the catalog's plain name. */
  private readonly titles = computed(
    () => new Map(this.classOptions().map((c) => [c.class_path, strategyTitle(c)])),
  );
  /** Class path to the catalog's description, the idea a run tests when none was written. */
  private readonly ideas = computed(
    () => new Map(this.classOptions().map((c) => [c.class_path, c.description])),
  );

  protected readonly offset = linkedSignal({ source: () => this.strategy(), computation: () => 0 });
  protected readonly runs = resource({
    params: () => ({
      strategy_class: this.strategy() || null,
      limit: PAGE_SIZE,
      offset: this.offset(),
    }),
    loader: ({ params }) => this.lab.ledgerRuns(params),
  });
  /** The last loaded page stays on screen while the next one loads. */
  protected readonly page = keepLatest(this.runs);

  /** Columns the viewer hid with the Columns menu. */
  protected readonly hidden = signal<string[]>([]);
  protected readonly shownColumns = computed(() => visibleColumns(this.columns, this.hidden()));
  protected readonly standfirst = computed(() => {
    const p = this.page();
    if (!p || !p.total)
      return 'Every lab run on record, the idea it tested and the settings it tried.';
    return `${p.total} lab ${p.total === 1 ? 'run' : 'runs'} on record. Open one for every setting it tried.`;
  });

  protected readonly columns: (TableColumn<LedgerRunView> & { fixed?: boolean })[] = [
    { key: 'started_at', label: 'Started', sortable: false, mobile: 'title', fixed: true },
    {
      key: 'strategy_class',
      label: 'Strategy',
      sortable: false,
      value: (r) => this.titles().get(r.strategy_class ?? '') ?? className(r.strategy_class),
    },
    {
      key: 'hypothesis',
      label: 'Idea',
      sortable: false,
      value: (r) => r.hypothesis || this.ideas().get(r.strategy_class ?? '') || '',
      // Long ideas wrap, so the Errors and Status columns stay in view at 1440px.
      wrap: true,
      mobile: 'hide',
    },
    { key: 'n_trials', label: 'Settings tried', format: 'number', sortable: false, help: false },
    { key: 'n_failed', label: 'Errors', format: 'number', sortable: false, mobile: 'hide' },
    {
      key: 'best_score',
      label: 'Best score',
      sortable: false,
      value: (r) => scoreText(r.best_score),
    },
    {
      key: 'robustness',
      label: 'Status',
      sortable: false,
      help: 'lab_verdict',
      value: (r) => robustnessWords(r.robustness).label,
    },
  ];

  protected setClass(value: string): void {
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { strategy: value || null },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }
}
