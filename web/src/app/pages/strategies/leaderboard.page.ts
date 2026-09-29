import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { GetLeaderboardData, LeaderboardRow, StrategyStatus } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { formatDate } from '../../core/format/format';
import { STATUS_WORDS } from '../../shared/governance-labels';
import { rowStrategyName, strategyKindName } from '../../shared/strategy-names';
import { type Verdict, strategyVerdict } from '../../shared/strategy-verdict';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { StrategyVerdict } from '../../shared/ui/strategy-verdict';

type SortKey = NonNullable<NonNullable<GetLeaderboardData['query']>['sort']>;

export const SORTS: readonly { id: SortKey; label: string }[] = [
  { id: 'sharpe', label: 'Risk-adjusted (Sharpe)' },
  { id: 'return', label: 'Total return' },
  { id: 'drawdown', label: 'Smallest drop' },
  { id: 'trades', label: 'Most trades' },
];

/** On trial, Approved or Retired: the status in the words people read. */
export function stageLabel(row: Pick<LeaderboardRow, 'status'>): string {
  return STATUS_WORDS[row.status as StrategyStatus] ?? row.status;
}

/** "Passed", "Failed" or "Not checked" for the go-live check (vocabulary status words). */
export function goliveLabel(passed: boolean | null | undefined): string {
  if (passed === true) return 'Passed';
  if (passed === false) return 'Failed';
  return 'Not checked';
}

/** The plain verdict for a row, from what the leaderboard knows. */
export function rowVerdict(r: LeaderboardRow): Verdict {
  return strategyVerdict({
    status: r.status as StrategyStatus,
    golive: null,
    golivePassed: r.golive_passed,
    trial: {
      total_return: r.paper.total_return ?? null,
      max_drawdown: r.paper.max_drawdown ?? null,
      days: r.paper.days,
    },
    tests: r.survival_total ? { passed: r.survival_passed, total: r.survival_total } : null,
  });
}

/**
 * The comparison view (F27): every strategy side by side, ranked by its
 * trial result on its own test book, with one plain verdict each. Each row
 * opens the strategy page, where one strategy is judged in full.
 */
@Component({
  selector: 'app-leaderboard-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    DataTable,
    TableCell,
    StatusPill,
    StrategyVerdict,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  template: `
    <app-page-header
      title="Leaderboard"
      description="Every strategy side by side, ranked by its trial result. Open one to judge it in full."
    >
      <a actions class="related-link" routerLink="/strategies">All strategies</a>
    </app-page-header>

    <section class="panel" aria-labelledby="board-title">
      <div class="panel-head head">
        <h2 id="board-title">Ranking</h2>
        <div class="filters">
          <label class="field-inline">
            <span>Rank by</span>
            <select class="input" (change)="sort.set($any($event.target).value)">
              @for (s of sorts; track s.id) {
                <option [value]="s.id" [selected]="sort() === s.id">{{ s.label }}</option>
              }
            </select>
          </label>
          <label class="field-inline">
            <input
              type="checkbox"
              class="check"
              [checked]="retired()"
              (change)="retired.set(!retired())"
            />
            <span>Show retired</span>
          </label>
        </div>
      </div>
      @if (board.error(); as err) {
        <app-error-state
          title="Could not load the leaderboard"
          [error]="err"
          (retry)="board.reload()"
        />
      } @else if (!board.hasValue()) {
        <app-loading-state label="Loading the leaderboard" [rows]="5" />
      } @else if (board.value().rows.length === 0) {
        <app-empty-state
          title="No strategies yet"
          message="Strategies show here once they pass their tests in Lab and go on trial."
        >
          <a routerLink="/lab" class="btn">Open Lab</a>
        </app-empty-state>
      } @else {
        @if (asOf(); as d) {
          <p class="as-of muted">
            Trial results up to <span class="num">{{ d }}</span
            >. Paper money on each strategy's own test book.
          </p>
        }
        <app-data-table
          caption="Strategies ranked by trial result"
          [rows]="board.value().rows"
          [columns]="columns"
          [rowKey]="rowKey"
          [pageSize]="25"
        >
          <ng-template appCell="strategy_id" [appCellOf]="board.value().rows" let-r>
            <a [routerLink]="['/strategies', r.strategy_id]" class="name">
              <span class="num rank">{{ r.rank }}</span>
              <span class="id">{{ name(r) }}</span>
              <span class="cls muted">{{ kind(r.class_path) }}</span>
            </a>
          </ng-template>
          <ng-template appCell="stage" [appCellOf]="board.value().rows" let-r>
            <app-status-pill [status]="r.status" />
          </ng-template>
          <ng-template appCell="verdict" [appCellOf]="board.value().rows" let-r>
            <app-strategy-verdict compact [verdict]="verdict(r)" />
          </ng-template>
        </app-data-table>
      }
    </section>
  `,
  styles: `
    .head {
      flex-wrap: wrap;
      gap: var(--space-2) var(--space-4);
    }
    .filters {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-4);
    }
    .field-inline {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--touch-min);
      font-size: var(--text-sm);
    }
    .field-inline select {
      max-width: 16rem;
    }
    .related-link {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      font-size: var(--text-sm);
    }
    .as-of {
      padding: var(--space-2) var(--space-4) 0;
      font-size: var(--text-sm);
    }
    .name {
      display: inline-grid;
      grid-template-columns: auto minmax(0, 1fr);
      column-gap: var(--space-2);
      align-items: baseline;
      align-content: center;
      min-height: var(--touch-min);
      text-decoration: none;
    }
    .rank {
      grid-row: span 2;
      color: var(--color-ink-3);
    }
    .id {
      font-weight: var(--weight-semibold);
      text-decoration: underline;
      text-underline-offset: 2px;
      overflow-wrap: anywhere;
    }
    .cls {
      font-size: var(--text-xs);
    }
  `,
})
export class LeaderboardPage {
  private readonly api = inject(StrategiesService);

  protected readonly sorts = SORTS;
  protected readonly sort = signal<SortKey>('sharpe');
  protected readonly retired = signal(false);

  protected readonly board = resource({
    params: () => ({ sort: this.sort(), include_retired: this.retired() }),
    loader: ({ params }) => this.api.leaderboard(params),
  });
  protected readonly asOf = computed(() => {
    const d = this.board.hasValue() ? this.board.value().as_of : null;
    return d ? formatDate(d) : null;
  });
  private readonly verdicts = computed(() => {
    const rows = this.board.hasValue() ? this.board.value().rows : [];
    return new Map(rows.map((r) => [r.strategy_id, rowVerdict(r)]));
  });

  protected readonly columns: TableColumn<LeaderboardRow>[] = [
    { key: 'strategy_id', label: 'Strategy', mobile: 'title', sortable: false },
    { key: 'verdict', label: 'Verdict', value: (r) => rowVerdict(r).label, sortable: false },
    { key: 'stage', label: 'Status', value: (r) => stageLabel(r), sortable: false },
    {
      key: 'total_return',
      label: 'Return',
      value: (r) => r.paper.total_return,
      format: 'signedPercent',
      tone: true,
      help: 'total_return',
    },
    {
      key: 'max_drawdown',
      label: 'Max drawdown',
      value: (r) => r.paper.max_drawdown,
      format: 'percent',
    },
    { key: 'sharpe', label: 'Sharpe', value: (r) => r.paper.sharpe, format: 'number' },
    {
      key: 'days',
      label: 'Days on trial',
      value: (r) => r.paper.days,
      format: 'number',
      mobile: 'hide',
      help: false,
    },
    {
      key: 'trades',
      label: 'Trades',
      value: (r) => r.paper.trades + r.book_trades,
      format: 'number',
      mobile: 'hide',
      help: false,
    },
    {
      key: 'tests',
      label: 'Robustness tests',
      value: (r) => (r.survival_total ? `${r.survival_passed} of ${r.survival_total}` : '–'),
      sortable: false,
      mobile: 'hide',
      help: false,
    },
  ];
  protected readonly rowKey = (r: LeaderboardRow) => r.strategy_id;
  protected readonly name = (r: LeaderboardRow) => rowStrategyName(r) ?? r.strategy_id;
  protected readonly kind = strategyKindName;

  protected verdict(r: LeaderboardRow): Verdict {
    return this.verdicts().get(r.strategy_id) ?? rowVerdict(r);
  }
}
