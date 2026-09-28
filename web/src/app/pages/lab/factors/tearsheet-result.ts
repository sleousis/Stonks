import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type {
  FactorTearSheetView,
  GroupIcView,
  HorizonSummaryView,
  MonthlyIcView,
} from '../../../api/models';
import { formatNumber } from '../../../core/format/format';
import type { ChartSeries } from '../../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../../shared/chart/time-series-chart';
import { numOrNa, pctOrNa } from '../../../shared/lab-results/result-figures';
import { DataTable, TableCell, type TableColumn } from '../../../shared/ui/data-table/data-table';
import { StatTile } from '../../../shared/ui/stat-tile';
import { directionText, divergingColor, maxMagnitude } from './factor-requests';

export const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
] as const;

const GROUP_TITLES: Record<string, string> = {
  sector: 'By sector',
  asset_class: 'By asset class',
  size: 'By size',
};

/** "1 bar", "21 bars". */
export function barsText(n: number): string {
  return `${formatNumber(n, { digits: 0 })} ${n === 1 ? 'bar' : 'bars'}`;
}

/** Bucket labels, lowest values first: "Q1 (lowest)" .. "Q5 (highest)". */
export function bucketLabel(i: number, n: number): string {
  if (i === 0) return `Q1 (lowest)`;
  if (i === n - 1) return `Q${n} (highest)`;
  return `Q${i + 1}`;
}

/** Colours of the bucket curves: bottom loss, top gain, the rest muted. */
function bucketColor(i: number, n: number): ChartSeries['color'] {
  if (i === 0) return 'loss';
  if (i === n - 1) return 'gain';
  return 'muted';
}

/**
 * A factor tear sheet: tiles, IC per horizon, the mean return of each
 * bucket as bars, the buckets' cumulative returns, IC by group, a monthly
 * IC heatmap and the long-short book's alpha and beta.
 *
 *   <app-factor-tearsheet-result [result]="view" />
 */
@Component({
  selector: 'app-factor-tearsheet-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, TableCell, StatTile, TimeSeriesChart],
  template: `
    @let r = result();
    <div class="tiles">
      <app-stat-tile
        label="Mean IC"
        featured
        [help]="false"
        [value]="ic(main()?.mean_ic)"
        [detail]="r.ic_horizon ? 'Next ' + bars(r.ic_horizon) : null"
      />
      <app-stat-tile label="t-stat" [help]="false" [value]="num(main()?.t_stat_hac)" />
      <app-stat-tile label="Names" [value]="count(r.n_tickers)" [detail]="coverageText()" />
      <app-stat-tile
        label="Dates"
        [value]="count(r.n_dates)"
        [detail]="'Every ' + bars(r.every_bars)"
      />
      <app-stat-tile
        label="Alpha a year"
        [help]="false"
        [value]="pct(r.alpha_beta?.alpha_annual, true)"
      />
      <app-stat-tile label="Beta" [value]="num(r.alpha_beta?.beta)" />
    </div>
    <p class="explain">
      {{ directionLine() }} Values are raw, so a factor where lower is better shows a negative IC
      when it works. A t-stat above 2 is unlikely to be luck.
    </p>
    @if (r.status !== 'ok') {
      <p class="note" role="status">
        Not enough data for a tear sheet.{{ r.note ? ' ' + r.note : '' }}
      </p>
    }

    @if (horizons().length) {
      <section aria-labelledby="ts-ic-title">
        <h3 id="ts-ic-title">IC per horizon</h3>
        <app-data-table
          caption="IC per horizon"
          [rows]="horizons()"
          [columns]="horizonColumns"
          [rowKey]="horizonKey"
          [pageSize]="0"
        >
          <ng-template appCell="horizon" [appCellOf]="horizons()" let-h>
            <span class="num">{{ bars(h.horizon) }}</span>
          </ng-template>
          <ng-template appCell="mean_ic" [appCellOf]="horizons()" let-h>
            <span class="num" [class.na]="na(h.mean_ic)">{{ ic(h.mean_ic) }}</span>
          </ng-template>
          <ng-template appCell="icir" [appCellOf]="horizons()" let-h>
            <span class="num" [class.na]="na(h.icir)">{{ num(h.icir) }}</span>
          </ng-template>
          <ng-template appCell="hit_rate" [appCellOf]="horizons()" let-h>
            <span class="num" [class.na]="na(h.hit_rate)">{{ pct(h.hit_rate) }}</span>
          </ng-template>
          <ng-template appCell="t_stat_hac" [appCellOf]="horizons()" let-h>
            <span class="num" [class.na]="na(h.t_stat_hac)">{{ num(h.t_stat_hac) }}</span>
          </ng-template>
          <ng-template appCell="spread_mean" [appCellOf]="horizons()" let-h>
            <span class="num" [class.na]="na(h.spread_mean)">{{ pct(h.spread_mean, true) }}</span>
          </ng-template>
        </app-data-table>
      </section>
    }

    @if (buckets().length) {
      <section aria-labelledby="ts-q-title">
        <h3 id="ts-q-title">Return per bucket</h3>
        <p class="muted small">
          Mean return over the next {{ bars(r.ic_horizon ?? 1) }} of each bucket, lowest values
          first.
        </p>
        <figure class="bars" aria-labelledby="ts-q-title">
          <table class="bar-table">
            <caption class="sr-only">
              Mean forward return per bucket
            </caption>
            <tbody>
              @for (b of buckets(); track b.label) {
                <tr>
                  <th scope="row">{{ b.label }}</th>
                  <td>
                    <span class="track" aria-hidden="true">
                      <span
                        class="bar"
                        [class.neg]="b.value !== null && b.value < 0"
                        [style.width.%]="b.width"
                        [style.margin-left.%]="b.offset"
                      ></span>
                    </span>
                    <span class="num value" [class.na]="b.value === null">{{
                      pct(b.value, true)
                    }}</span>
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </figure>
      </section>
    }

    @if (curves().length) {
      <section aria-labelledby="ts-curves-title">
        <h3 id="ts-curves-title">Cumulative return per bucket</h3>
        <app-time-series-chart
          ariaLabel="Cumulative return of each bucket and of top minus bottom"
          [summary]="curveSummary()"
          [series]="curves()"
          [height]="280"
        />
        <p class="muted small legend">
          <span class="swatch gain"></span> Top bucket <span class="swatch loss"></span> Bottom
          bucket <span class="swatch primary"></span> Top minus bottom
          <span class="swatch muted"></span> Buckets in between
        </p>
      </section>
    }

    @if (groups().length) {
      <section aria-labelledby="ts-groups-title">
        <h3 id="ts-groups-title">IC by group</h3>
        <p class="muted small">
          Does it work everywhere, or only in one corner? Sectors and asset classes are today’s
          labels. Size is {{ r.size_basis === 'market_cap' ? 'market cap' : 'dollar volume' }}.
        </p>
        <div class="groups">
          @for (g of groups(); track g.key) {
            <app-data-table
              [caption]="g.title"
              [rows]="g.rows"
              [columns]="groupColumns"
              [rowKey]="groupKey"
              [pageSize]="0"
            >
              <ng-template appCell="mean_ic" [appCellOf]="g.rows" let-row>
                <span class="num" [class.na]="na(row.mean_ic)">{{ ic(row.mean_ic) }}</span>
              </ng-template>
              <ng-template appCell="t_stat_hac" [appCellOf]="g.rows" let-row>
                <span class="num" [class.na]="na(row.t_stat_hac)">{{ num(row.t_stat_hac) }}</span>
              </ng-template>
            </app-data-table>
          }
        </div>
      </section>
    }

    @if (monthly().length) {
      <section aria-labelledby="ts-monthly-title">
        <h3 id="ts-monthly-title">Monthly IC</h3>
        <p class="muted small">Is the edge steady, or a few good months? Green is above 0.</p>
        <div class="scroll" tabindex="0" role="region" aria-label="Monthly IC, scrolls sideways">
          <table class="heat">
            <caption class="sr-only">
              Mean IC per month
            </caption>
            <thead>
              <tr>
                <th scope="col">Year</th>
                @for (m of months; track m) {
                  <th scope="col">{{ m }}</th>
                }
              </tr>
            </thead>
            <tbody>
              @for (row of monthly(); track row.year) {
                <tr>
                  <th scope="row" class="num">{{ row.year }}</th>
                  @for (v of row.months; track $index) {
                    <td class="num" [class.na]="na(v)" [style.background]="heat(v)">
                      {{ na(v) ? '' : ic(v) }}
                    </td>
                  }
                </tr>
              }
            </tbody>
          </table>
        </div>
      </section>
    }

    @if (r.alpha_beta; as ab) {
      <section aria-labelledby="ts-alpha-title">
        <h3 id="ts-alpha-title">Alpha and beta</h3>
        <p class="muted small">
          The long-short book against {{ ab.benchmark || 'the equal-weight universe' }}.
        </p>
        <dl class="facts">
          <div>
            <dt>Alpha a year</dt>
            <dd class="num">{{ pct(ab.alpha_annual, true) }}</dd>
          </div>
          <div>
            <dt>Alpha t-stat</dt>
            <dd class="num">{{ num(ab.alpha_t) }}</dd>
          </div>
          <div>
            <dt>Beta</dt>
            <dd class="num">{{ num(ab.beta) }}</dd>
          </div>
          <div>
            <dt>R squared</dt>
            <dd class="num">{{ num(ab.r_squared) }}</dd>
          </div>
          <div>
            <dt>Periods</dt>
            <dd class="num">{{ count(ab.n_periods ?? 0) }}</dd>
          </div>
          <div>
            <dt>Turnover of the ranking</dt>
            <dd class="num">{{ pct(r.score_turnover) }}</dd>
          </div>
          <div>
            <dt>Turnover of the top bucket</dt>
            <dd class="num">{{ pct(r.top_quantile_turnover) }}</dd>
          </div>
        </dl>
      </section>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-5, var(--space-4));
      min-width: 0;
    }
    section {
      display: grid;
      gap: var(--space-2);
      min-width: 0;
    }
    h3 {
      font-size: var(--text-md);
      margin: 0;
    }
    .tiles {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 8rem), 1fr));
    }
    .explain,
    .note,
    .small {
      margin: 0;
    }
    .small {
      font-size: var(--text-sm);
    }
    .note {
      padding: var(--space-3) var(--space-4);
      border-radius: var(--radius-md);
      background: var(--color-surface-2);
    }
    .na {
      color: var(--color-ink-3);
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }
    .bars {
      margin: 0;
    }
    .bar-table {
      width: 100%;
      border-collapse: collapse;
      th {
        width: 7.5rem;
        padding: var(--space-1) var(--space-2) var(--space-1) 0;
        text-align: left;
        font-weight: var(--weight-regular);
        font-size: var(--text-sm);
        color: var(--color-ink-2);
        white-space: nowrap;
      }
      td {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        padding: var(--space-1) 0;
      }
    }
    .track {
      position: relative;
      flex: 1 1 auto;
      height: 14px;
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
      overflow: hidden;
    }
    .bar {
      display: block;
      height: 100%;
      background: var(--color-gain);
    }
    .bar.neg {
      background: var(--color-loss);
    }
    .value {
      flex: 0 0 5rem;
      text-align: right;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-2);
    }
    .swatch {
      display: inline-block;
      width: 12px;
      height: 3px;
      border-radius: 2px;
      &.gain {
        background: var(--color-gain);
      }
      &.loss {
        background: var(--color-loss);
      }
      &.primary {
        background: var(--color-accent);
      }
      &.muted {
        background: var(--color-ink-3);
      }
    }
    .groups {
      display: grid;
      gap: var(--space-4);
      @include bp.from-desktop {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .scroll {
      overflow-x: auto;
      max-width: 100%;
    }
    .heat {
      border-collapse: separate;
      border-spacing: 2px;
      font-size: var(--text-xs);
      th {
        padding: var(--space-1);
        font-weight: var(--weight-medium);
        color: var(--color-ink-2);
      }
      td {
        min-width: 3rem;
        padding: var(--space-1);
        text-align: center;
        border-radius: var(--radius-sm);
        background: var(--color-surface-2);
      }
    }
    .facts {
      display: grid;
      gap: var(--space-2) var(--space-4);
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 9rem), 1fr));
      margin: 0;
      dt {
        font-size: var(--text-xs);
        color: var(--color-ink-3);
      }
      dd {
        margin: 0;
        font-weight: var(--weight-medium);
      }
    }
  `,
})
export class FactorTearsheetResult {
  readonly result = input.required<FactorTearSheetView>();

  protected readonly months = MONTHS;

  protected readonly horizons = computed(() =>
    [...(this.result().horizons ?? [])].sort((a, b) => a.horizon - b.horizon),
  );
  /** The horizon the rest of the sheet uses (21 bars when asked for). */
  protected readonly main = computed(() => {
    const h = this.result().ic_horizon;
    return this.horizons().find((x) => x.horizon === h) ?? this.horizons().at(-1) ?? null;
  });

  protected readonly coverageText = computed(() => {
    const c = this.result().coverage;
    return c == null ? null : `${pctOrNa(c)} with a value`;
  });

  protected readonly directionLine = computed(() => {
    const f = this.result().factor;
    return `${f.id}: ${directionText(f.direction).toLowerCase()}.`;
  });

  /** One bar per bucket, drawn from a zero line so negative bars go left. */
  protected readonly buckets = computed(() => {
    const means = this.main()?.quantile_means ?? [];
    const scale = maxMagnitude(means);
    const hasNeg = means.some((v) => v != null && v < 0);
    const zero = hasNeg ? 50 : 0;
    const span = hasNeg ? 50 : 100;
    return means.map((v, i) => {
      const share = v == null || scale === 0 ? 0 : (Math.abs(v) / scale) * span;
      return {
        label: bucketLabel(i, means.length),
        value: v ?? null,
        width: share,
        offset: v != null && v < 0 ? zero - share : zero,
      };
    });
  });

  protected readonly curves = computed<ChartSeries[]>(() => {
    const q = this.result().quantile_curves;
    const dates = q?.dates ?? [];
    if (!q || dates.length === 0) return [];
    const points = (values: readonly (number | null)[]) =>
      dates
        .map((time, i) => ({ time, value: values[i] }))
        .filter((p): p is { time: string; value: number } => p.value != null);
    const n = q.series?.length ?? 0;
    const series: ChartSeries[] = (q.series ?? []).map((values, i) => ({
      id: `q${i + 1}`,
      label: bucketLabel(i, n),
      kind: 'line',
      color: bucketColor(i, n),
      format: 'percent',
      points: points(values),
    }));
    if (q.spread?.length) {
      series.push({
        id: 'spread',
        label: 'Top minus bottom',
        kind: 'line',
        color: 'primary',
        format: 'percent',
        points: points(q.spread),
      });
    }
    return series;
  });

  protected readonly curveSummary = computed(() => {
    const spread = this.result().quantile_curves?.spread ?? [];
    const last = spread.at(-1);
    return last == null
      ? 'Cumulative return of each bucket over the window.'
      : `Top minus bottom ended the window at ${pctOrNa(last, true)}.`;
  });

  protected readonly groups = computed(() =>
    Object.entries(this.result().ic_by_group ?? {})
      .filter(([, rows]) => rows.length > 0)
      .map(([key, rows]) => ({ key, title: GROUP_TITLES[key] ?? key, rows })),
  );

  protected readonly monthly = computed<MonthlyIcView[]>(() =>
    [...(this.result().monthly_ic ?? [])].sort((a, b) => a.year - b.year),
  );
  private readonly monthlyScale = computed(() =>
    maxMagnitude(this.monthly().flatMap((r) => r.months)),
  );

  protected readonly horizonKey = (h: HorizonSummaryView) => String(h.horizon);
  protected readonly groupKey = (g: GroupIcView) => g.group;

  protected readonly horizonColumns: TableColumn<HorizonSummaryView>[] = [
    { key: 'horizon', label: 'Next', mobile: 'title', help: false },
    { key: 'mean_ic', label: 'Mean IC', align: 'end', help: false },
    { key: 'icir', label: 'ICIR', align: 'end', help: false },
    { key: 'hit_rate', label: 'Dates with IC above 0', align: 'end', help: false },
    { key: 't_stat_hac', label: 't-stat', align: 'end', help: false },
    { key: 'spread_mean', label: 'Top minus bottom', align: 'end', help: false },
  ];

  protected readonly groupColumns: TableColumn<GroupIcView>[] = [
    { key: 'group', label: 'Group', mobile: 'title', help: false },
    { key: 'mean_ic', label: 'Mean IC', align: 'end', help: false },
    { key: 't_stat_hac', label: 't-stat', align: 'end', help: false },
    { key: 'n_dates', label: 'Dates', format: 'number', help: false },
    {
      key: 'mean_names',
      label: 'Names a date',
      format: 'number',
      mobile: 'hide',
      help: false,
    },
  ];

  protected heat(v: number | null | undefined): string | null {
    return divergingColor(v, this.monthlyScale());
  }

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
    return barsText(n);
  }
}
