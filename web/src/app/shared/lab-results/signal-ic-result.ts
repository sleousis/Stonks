import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { HorizonIcView, SignalIcView } from '../../api/models';
import { formatNumber } from '../../core/format/format';
import { DataTable, TableCell, type TableColumn } from '../ui/data-table/data-table';
import { StatTile } from '../ui/stat-tile';
import { numOrNa, pctOrNa } from './result-figures';

/** One plain sentence under the table: what IC means. */
export const IC_EXPLANATION =
  'IC shows how well the strategy’s scores ranked the moves that came next across tickers. ' +
  '0 means no skill, higher is better, and below 0 means it ranked them the wrong way round.';

/**
 * A signal IC result: the IC per forward horizon (the decay curve as a
 * table), the average move per score bucket, and how often the scores change.
 *
 *   <app-signal-ic-result [result]="view" />
 */
@Component({
  selector: 'app-signal-ic-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, TableCell, StatTile],
  template: `
    @let r = result();
    <div class="summary">
      <app-stat-tile
        label="IC estimate"
        featured
        [help]="false"
        [value]="ic(r.ic_estimate)"
        [detail]="r.ic_horizon ? 'Next ' + bars(r.ic_horizon) : null"
      />
      <app-stat-tile label="Tickers" [value]="count(r.n_tickers)" />
      <app-stat-tile
        label="Dates scored"
        [value]="count(r.n_dates)"
        [detail]="'Every ' + bars(r.every_bars)"
      />
      <app-stat-tile label="Score turnover" [value]="pct(r.score_turnover)" />
    </div>
    <p class="explain">{{ explanation }}</p>
    @if (r.status !== 'ok') {
      <p class="note" role="status">
        Not enough data to measure the signal.{{ r.note ? ' ' + r.note : '' }}
      </p>
    }
    @if (rows().length) {
      <app-data-table
        caption="IC by horizon"
        [rows]="rows()"
        [columns]="columns"
        [rowKey]="rowKey"
        [pageSize]="0"
      >
        <ng-template appCell="horizon" [appCellOf]="rows()" let-h>
          <span class="num">{{ bars(h.horizon) }}</span>
        </ng-template>
        <ng-template appCell="mean_ic" [appCellOf]="rows()" let-h>
          <span class="num" [class.na]="na(h.mean_ic)">{{ ic(h.mean_ic) }}</span>
        </ng-template>
        <ng-template appCell="t_stat_hac" [appCellOf]="rows()" let-h>
          <span class="num" [class.na]="na(h.t_stat_hac)">{{ num(h.t_stat_hac) }}</span>
        </ng-template>
        <ng-template appCell="hit_rate" [appCellOf]="rows()" let-h>
          <span class="num" [class.na]="na(h.hit_rate)">{{ pct(h.hit_rate) }}</span>
        </ng-template>
        <ng-template appCell="spread_mean" [appCellOf]="rows()" let-h>
          <span class="num" [class.na]="na(h.spread_mean)">{{ pct(h.spread_mean, true) }}</span>
        </ng-template>
        <ng-template appCell="buckets" [appCellOf]="rows()" let-h>
          <span class="num buckets">{{ buckets(h) }}</span>
        </ng-template>
      </app-data-table>
      <p class="muted foot">
        Top minus bottom is the average next move of the highest scored tickers minus the lowest.
        The buckets run from the lowest scores to the highest. A t-stat above 2 is unlikely to be
        luck.
      </p>
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
    .explain,
    .note,
    .foot {
      margin: 0;
    }
    .note {
      padding: var(--space-3) var(--space-4);
      border-radius: var(--radius-md);
      background: var(--color-surface-2);
    }
    .foot {
      font-size: var(--text-xs);
    }
    .na {
      color: var(--color-ink-3);
    }
    .buckets {
      white-space: normal;
    }
  `,
})
export class SignalIcResult {
  readonly result = input.required<SignalIcView>();

  protected readonly explanation = IC_EXPLANATION;
  protected readonly rows = computed(() =>
    [...(this.result().horizons ?? [])].sort((a, b) => a.horizon - b.horizon),
  );
  protected readonly rowKey = (h: HorizonIcView) => String(h.horizon);
  protected readonly columns: TableColumn<HorizonIcView>[] = [
    { key: 'horizon', label: 'Next', mobile: 'title', help: false },
    { key: 'mean_ic', label: 'Mean IC', align: 'end', help: false },
    { key: 't_stat_hac', label: 't-stat', align: 'end', help: false },
    { key: 'hit_rate', label: 'Dates with IC above 0', align: 'end', help: false },
    { key: 'spread_mean', label: 'Top minus bottom', align: 'end', help: false },
    {
      key: 'buckets',
      label: 'By score bucket',
      sortable: false,
      mobile: 'hide',
      help: false,
    },
  ];

  /** Not computable: shown as n/a in a muted colour. */
  protected na(v: number | null | undefined): boolean {
    return typeof v !== 'number' || !Number.isFinite(v);
  }

  protected ic(v: number | null | undefined): string {
    return numOrNa(v, 3);
  }

  protected num(v: number | null | undefined): string {
    return numOrNa(v, 2);
  }

  protected pct(v: number | null | undefined, signed = false): string {
    return pctOrNa(v, signed);
  }

  protected count(n: number): string {
    return formatNumber(n, { digits: 0 });
  }

  protected bars(n: number): string {
    return `${formatNumber(n, { digits: 0 })} ${n === 1 ? 'bar' : 'bars'}`;
  }

  protected buckets(h: HorizonIcView): string {
    return h.quantile_means.map((v) => pctOrNa(v, true)).join(' · ');
  }
}
