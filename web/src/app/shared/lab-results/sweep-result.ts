import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { SweepResultView, SweepRowView } from '../../api/models';
import { formatNumber } from '../../core/format/format';
import { NA } from '../metrics';
import { DataTable, TableCell, type TableColumn } from '../ui/data-table/data-table';
import { StatTile } from '../ui/stat-tile';
import { humanize } from '../ui/param-form/param-spec';
import { StatusPill } from '../ui/status-pill';
import { numOrNa } from './result-figures';

/** A sweep row with its rank (1 is best) and survival tests passed. */
export interface RankedSweepRow extends SweepRowView {
  rank: number;
  testsPassed: number;
  testsRun: number;
}

const VERDICT_ORDER: Record<SweepRowView['verdict'], number> = { pass: 0, fail: 1, error: 2 };

function testsPassed(row: SweepRowView): [number, number] {
  const tests = Object.values(row.survival ?? {});
  return [tests.filter((t) => t['passed'] === true).length, tests.length];
}

/**
 * Best first: passed runs, then failed, then errors; within each, the
 * higher tuning score first and missing scores last.
 */
export function rankSweepRows(rows: readonly SweepRowView[]): RankedSweepRow[] {
  const score = (r: SweepRowView) =>
    typeof r.best_score === 'number' && Number.isFinite(r.best_score) ? r.best_score : -Infinity;
  return [...rows]
    .sort(
      (a, b) =>
        VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict] ||
        score(b) - score(a) ||
        a.strategy.localeCompare(b.strategy),
    )
    .map((r, i) => {
      const [passed, run] = testsPassed(r);
      return { ...r, rank: i + 1, testsPassed: passed, testsRun: run };
    });
}

/**
 * A sweep's rows: every strategy (or strategy and ticker) the lab ran,
 * ranked best first, sortable by any column.
 *
 *   <app-sweep-result [result]="view" />
 */
@Component({
  selector: 'app-sweep-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, TableCell, StatTile, StatusPill],
  template: `
    @let r = result();
    <div class="summary">
      <app-stat-tile label="Passed" [value]="count(r.passed)" detailTone="gain" />
      <app-stat-tile label="Failed" [value]="count(r.failed)" />
      <app-stat-tile label="Errors" [value]="count(r.errors)" />
      <app-stat-tile
        label="Basket"
        [value]="count(r.universe.length)"
        [detail]="r.universe.length === 1 ? 'ticker' : 'tickers'"
      />
    </div>
    <p class="lead">
      Each strategy was tuned and tested on the same data. Nothing went on trial, and every setting
      tried counts in the trial ledger.
    </p>
    @if (rows().length === 0) {
      <p class="muted">The sweep found no strategy to run on this basket.</p>
    } @else {
      <app-data-table
        caption="Sweep results, best first"
        [rows]="rows()"
        [columns]="columns"
        [rowKey]="rowKey"
        [initialSort]="{ key: 'rank', dir: 'asc' }"
        [pageSize]="25"
      >
        <ng-template appCell="strategy" [appCellOf]="rows()" let-row>
          <span class="name">{{ name(row.strategy) }}</span>
          @if (row.ticker) {
            <span class="muted ticker">{{ row.ticker }}</span>
          }
        </ng-template>
        <ng-template appCell="verdict" [appCellOf]="rows()" let-row>
          <app-status-pill [status]="row.verdict" />
          @if (row.error) {
            <span class="row-error">{{ row.error }}</span>
          }
        </ng-template>
        <ng-template appCell="best_score" [appCellOf]="rows()" let-row>
          <span class="num" [class.na]="na(row.best_score)">{{ score(row.best_score) }}</span>
        </ng-template>
        <ng-template appCell="tests" [appCellOf]="rows()" let-row>
          <span class="num" [class.na]="row.testsRun === 0">{{ testsText(row) }}</span>
        </ng-template>
      </app-data-table>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .summary {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 7.5rem), 1fr));
    }
    .lead {
      margin: 0;
      color: var(--color-ink-2);
    }
    .name {
      font-weight: 600;
    }
    .ticker {
      margin-left: var(--space-2);
      font-size: var(--text-xs);
    }
    .row-error {
      display: block;
      margin-top: var(--space-1);
      font-size: var(--text-xs);
      color: var(--color-ink-3);
      overflow-wrap: anywhere;
    }
    .na {
      color: var(--color-ink-3);
    }
  `,
})
export class SweepResult {
  readonly result = input.required<SweepResultView>();
  /** Strategy id to its plain name (the catalog's titles); unknown ids read in words. */
  readonly titles = input<ReadonlyMap<string, string>>(new Map());

  protected name(id: string): string {
    return this.titles().get(id) ?? humanize(id);
  }

  protected readonly rows = computed(() => rankSweepRows(this.result().rows));
  protected readonly rowKey = (r: RankedSweepRow) => `${r.strategy}|${r.ticker ?? ''}`;
  protected readonly columns: TableColumn<RankedSweepRow>[] = [
    { key: 'rank', label: 'Rank', format: 'number', mobile: 'hide', help: false },
    {
      key: 'strategy',
      label: 'Strategy',
      mobile: 'title',
      help: false,
      value: (r) => this.name(r.strategy),
    },
    {
      key: 'verdict',
      label: 'Robustness verdict',
      value: (r) => VERDICT_ORDER[r.verdict],
      help: 'lab_verdict',
    },
    { key: 'best_score', label: 'Best score', align: 'end', help: false },
    {
      key: 'tests',
      label: 'Tests passed',
      value: (r) => (r.testsRun ? r.testsPassed / r.testsRun : null),
      align: 'end',
      help: false,
    },
    { key: 'n_trials', label: 'Trials', format: 'number', help: 'trials' },
  ];

  /** Not computable: shown as n/a in a muted colour. */
  protected na(v: number | null | undefined): boolean {
    return typeof v !== 'number' || !Number.isFinite(v);
  }

  protected count(n: number): string {
    return formatNumber(n, { digits: 0 });
  }

  protected score(v: number | null | undefined): string {
    return numOrNa(v ?? null, 2);
  }

  protected testsText(r: RankedSweepRow): string {
    return r.testsRun ? `${r.testsPassed} of ${r.testsRun}` : NA;
  }
}
