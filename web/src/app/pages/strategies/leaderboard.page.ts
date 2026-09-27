import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { GetLeaderboardData, LeaderboardRow } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { formatDate } from '../../core/format/format';
import { STATUS_WORDS, STAGES, stageOf } from '../../shared/governance-labels';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { className } from '../lab/ledger.page';

type SortKey = NonNullable<NonNullable<GetLeaderboardData['query']>['sort']>;

export const SORTS: readonly { id: SortKey; label: string }[] = [
  { id: 'sharpe', label: 'Sharpe (risk-adjusted)' },
  { id: 'return', label: 'Total return' },
  { id: 'drawdown', label: 'Smallest drawdown' },
  { id: 'trades', label: 'Most trades' },
];

/** Paper, Ready, Live or Stopped, from the status and the go-live verdict. */
export function stageLabel(row: Pick<LeaderboardRow, 'status' | 'golive_passed'>): string {
  if (row.status === 'retired') return STATUS_WORDS.retired;
  const status = row.status as 'active' | 'shadow';
  const stage = stageOf(status, row.golive_passed);
  return STAGES.find((s) => s.id === stage)?.label ?? row.status;
}

/** "Passed", "Not yet" or a dash when the gate did not run. */
export function goliveLabel(passed: boolean | null | undefined): string {
  if (passed === true) return 'Passed';
  if (passed === false) return 'Not yet';
  return '–';
}

/**
 * Strategies side by side, ranked by their risk-adjusted paper result: the
 * model book the daily run keeps for each. Each row links to its tear sheet.
 */
@Component({
  selector: 'app-leaderboard-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PageHeader, DataTable, TableCell, LoadingState, EmptyState, ErrorState],
  template: `
    <app-page-header
      title="Leaderboard"
      description="Every strategy ranked by its paper result, adjusted for risk. Open one for its tear sheet."
    >
      <a actions class="btn btn-ghost" routerLink="/strategies">All strategies</a>
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
            <span>Show stopped</span>
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
          message="Strategies show here once the lab registers them and they trade on paper."
        >
          <a routerLink="/lab" class="btn">Go to the lab</a>
        </app-empty-state>
      } @else {
        @if (asOf(); as d) {
          <p class="as-of muted">
            Paper results up to <span class="num">{{ d }}</span
            >.
          </p>
        }
        <app-data-table
          caption="Strategies ranked by paper result"
          [rows]="board.value().rows"
          [columns]="columns"
          [rowKey]="rowKey"
          [pageSize]="25"
        >
          <ng-template appCell="strategy_id" [appCellOf]="board.value().rows" let-r>
            <a [routerLink]="['/strategies', r.strategy_id, 'tearsheet']" class="name">
              <span class="num rank">{{ r.rank }}</span>
              <span class="id">{{ r.strategy_id }}</span>
              <span class="cls muted">{{ cls(r.class_path) }}</span>
            </a>
          </ng-template>
          <ng-template appCell="stage" [appCellOf]="board.value().rows" let-r>
            <span class="stage" [attr.data-status]="r.status">{{ stage(r) }}</span>
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
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--control-h);
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
      min-height: var(--touch-min);
      align-content: center;
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
    .stage {
      display: inline-block;
      padding: 0 var(--space-2);
      border: 1px solid var(--color-border-strong);
      border-radius: var(--radius-pill);
      font-size: var(--text-xs);
      white-space: nowrap;
    }
    .stage[data-status='active'] {
      border-color: var(--color-ink);
      background: var(--color-ink);
      color: var(--color-surface);
    }
    .stage[data-status='retired'] {
      border-style: dashed;
      color: var(--color-ink-3);
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

  protected readonly columns: TableColumn<LeaderboardRow>[] = [
    { key: 'strategy_id', label: 'Strategy', mobile: 'title', sortable: false },
    { key: 'stage', label: 'Stage', value: (r) => stageLabel(r), sortable: false },
    { key: 'sharpe', label: 'Sharpe', value: (r) => r.paper.sharpe, format: 'number' },
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
    { key: 'days', label: 'Days', value: (r) => r.paper.days, format: 'number', mobile: 'hide' },
    {
      key: 'trades',
      label: 'Trades',
      value: (r) => r.paper.trades + r.book_trades,
      format: 'number',
      help: false,
    },
    {
      key: 'tests',
      label: 'Tests passed',
      value: (r) => (r.survival_total ? `${r.survival_passed} of ${r.survival_total}` : '–'),
      sortable: false,
      mobile: 'hide',
      help: false,
    },
    {
      key: 'golive',
      label: 'Go-live check',
      value: (r) => goliveLabel(r.golive_passed),
      sortable: false,
      help: false,
    },
  ];
  protected readonly rowKey = (r: LeaderboardRow) => r.strategy_id;

  protected stage(r: LeaderboardRow): string {
    return stageLabel(r);
  }

  protected cls(path: string): string {
    return className(path);
  }
}
