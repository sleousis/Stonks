import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { HeatmapView } from '../../api/models';
import { formatNumber } from '../../core/format/format';
import { formatMetric, metricLabel } from '../metrics';
import { humanize } from '../ui/param-form/param-spec';
import { StatusPill } from '../ui/status-pill';

/** Strongest tint a cell gets, so its figure stays readable in both themes. */
const MAX_TINT = 45;

export interface HeatCell {
  key: string;
  text: string;
  na: boolean;
  best: boolean;
  plateau: boolean;
  /** CSS background, or '' for no tint. */
  background: string;
  label: string;
}

export interface HeatRow {
  key: string;
  label: string;
  cells: HeatCell[];
}

/** A parameter value as the axis shows it. */
export function axisText(v: unknown): string {
  if (typeof v === 'number') return formatNumber(v);
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

/** The metric the cells were scored on, in words. */
export function heatmapMetricText(metric: string): string {
  return METRIC_WORDS[metric] ?? metricLabel(metric);
}

const METRIC_WORDS: Record<string, string> = {
  fast_sharpe: 'Sharpe on the fast path',
  sharpe: 'Sharpe',
  cagr: 'CAGR',
  final_return: 'Total return',
  sortino: 'Sortino',
  calmar: 'Calmar',
  sharpe_dd: 'Sharpe less twice the drawdown',
  multi: 'Sharpe, Calmar and drawdown together',
  cv_sharpe: 'Sharpe on purged folds',
  cv_cagr: 'CAGR on purged folds',
  cv_final_return: 'Total return on purged folds',
};

function same(a: unknown, b: unknown): boolean {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-9;
  return a === b;
}

function within(v: unknown, range: readonly number[] | null | undefined): boolean {
  if (!range || range.length !== 2 || typeof v !== 'number') return false;
  return v >= range[0] - 1e-9 && v <= range[1] + 1e-9;
}

/**
 * The cell tint: gain for high scores, loss for low ones. With scores on
 * both sides of 0 the scale centres on 0, else on the middle of the range.
 */
export function cellTint(score: number, min: number, max: number): string {
  if (!(max > min)) return '';
  const mid = min < 0 && max > 0 ? 0 : (min + max) / 2;
  const span = score >= mid ? max - mid : mid - min;
  if (!(span > 0)) return '';
  const t = Math.min(1, Math.abs(score - mid) / span);
  const pct = Math.round(t * MAX_TINT);
  if (pct === 0) return '';
  const color = score >= mid ? 'var(--color-gain)' : 'var(--color-loss)';
  return `color-mix(in srgb, ${color} ${pct}%, var(--color-surface))`;
}

/** Rows of cells for the table: scores, tints, the tuned cell and the plateau. */
export function heatmapRows(h: HeatmapView): HeatRow[] {
  const finite = h.scores.flat().filter((v): v is number => typeof v === 'number');
  const min = finite.length ? Math.min(...finite) : 0;
  const max = finite.length ? Math.max(...finite) : 0;
  const xName = humanize(h.x);
  const yName = humanize(h.y);
  return h.y_values.map((yv, i) => ({
    key: `y${i}`,
    label: axisText(yv),
    cells: h.x_values.map((xv, j) => {
      const score = h.scores[i]?.[j] ?? null;
      const na = typeof score !== 'number';
      const best = same(h.best[h.x], xv) && same(h.best[h.y], yv);
      const plateau = !!h.plateau && within(xv, h.plateau.x_range) && within(yv, h.plateau.y_range);
      const text = na ? 'n/a' : formatNumber(score, { digits: 2 });
      const parts = [`${xName} ${axisText(xv)}, ${yName} ${axisText(yv)}: ${text}`];
      if (best) parts.push('the tuned set');
      if (plateau) parts.push('in the plateau neighbourhood');
      return {
        key: `x${j}`,
        text,
        na,
        best,
        plateau,
        background: na ? '' : cellTint(score, min, max),
        label: parts.join(', '),
      };
    }),
  }));
}

/**
 * A 2D parameter sweep around the tuned set, with the plateau test's
 * verdict and neighbourhood drawn on it.
 *
 *   <app-param-heatmap [heatmap]="result.heatmap" />
 */
@Component({
  selector: 'app-param-heatmap',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  template: `
    @let h = heatmap();
    <p class="meta">
      Scored by <strong>{{ metric() }}</strong
      >{{ h.fast ? ', on the fast path' : ', with full backtests' }}.
      @if (fixed()) {
        <span class="fixed">Held at the tuned values: {{ fixed() }}.</span>
      }
    </p>

    @if (h.plateau; as p) {
      <div class="plateau" [class.failed]="!p.passed">
        <div class="plateau-head">
          <span class="plateau-name">Plateau test</span>
          <app-status-pill [status]="p.passed ? 'pass' : 'fail'" />
        </div>
        <p class="plateau-text">
          Neighbours within {{ stepText() }} of each range around the tuned set.
          {{ p.notes }}
        </p>
        @if (plateauMetrics().length) {
          <dl class="plateau-metrics">
            @for (m of plateauMetrics(); track m.key) {
              <div>
                <dt>{{ m.label }}</dt>
                <dd class="num">{{ m.value }}</dd>
              </div>
            }
          </dl>
        }
      </div>
    }

    <div class="wrap" role="region" tabindex="0" [attr.aria-label]="caption()">
      <table>
        <caption>
          {{
            caption()
          }}
        </caption>
        <thead>
          <tr>
            <th scope="col" class="corner">
              <span class="axis-y">{{ yName() }}</span>
              <span class="axis-x">{{ xName() }}</span>
            </th>
            @for (x of xLabels(); track $index) {
              <th scope="col" class="num">{{ x }}</th>
            }
          </tr>
        </thead>
        <tbody>
          @for (row of rows(); track row.key) {
            <tr>
              <th scope="row" class="num">{{ row.label }}</th>
              @for (c of row.cells; track c.key) {
                <td
                  class="num cell"
                  [class.na]="c.na"
                  [class.best]="c.best"
                  [class.plateau]="c.plateau"
                  [style.background]="c.background || null"
                  [attr.aria-label]="c.label"
                >
                  {{ c.text }}
                  @if (c.best) {
                    <span class="visually-hidden">, tuned</span>
                  }
                </td>
              }
            </tr>
          }
        </tbody>
      </table>
    </div>

    <ul class="legend" aria-label="Heatmap legend">
      <li><span class="swatch low" aria-hidden="true"></span>Lower score</li>
      <li><span class="swatch high" aria-hidden="true"></span>Higher score</li>
      <li><span class="swatch tuned" aria-hidden="true"></span>Tuned set</li>
      @if (h.plateau) {
        <li><span class="swatch hood" aria-hidden="true"></span>Plateau neighbourhood</li>
      }
    </ul>
    <p class="foot">
      Every cell counts as a trial, and the winner is still the tuner's pick. A broad high area is a
      good sign. A lone bright cell may be luck.
    </p>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .meta,
    .foot,
    .plateau-text {
      margin: 0;
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .fixed {
      display: block;
      color: var(--color-ink-2);
    }
    .foot {
      font-size: var(--text-xs);
      color: var(--color-ink-2);
    }
    .plateau {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-gain);
      border-radius: var(--radius-sm);
      background: var(--color-surface);
    }
    .plateau.failed {
      border-left-color: var(--color-loss);
    }
    .plateau-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    .plateau-name {
      font-weight: var(--weight-semibold);
    }
    .plateau-metrics {
      margin: 0;
      display: grid;
      gap: var(--space-2) var(--space-4);
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 7.5rem), 1fr));
    }
    dt {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    dd {
      margin: 0;
      font-weight: var(--weight-medium);
    }
    .wrap {
      max-width: 100%;
      overflow-x: auto;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
    }
    .wrap:focus-visible {
      outline: 2px solid var(--color-accent);
      outline-offset: 2px;
    }
    table {
      border-collapse: separate;
      border-spacing: 2px;
      font-size: var(--text-xs);
      margin: 0 auto;
    }
    caption {
      caption-side: top;
      text-align: start;
      padding: var(--space-2);
      color: var(--color-ink-2);
      font-size: var(--text-xs);
    }
    th {
      font-weight: var(--weight-medium);
      color: var(--color-ink-2);
      padding: var(--space-1) var(--space-2);
      white-space: nowrap;
    }
    .corner {
      text-align: start;
      font-size: 0.7rem;
      line-height: 1.2;
    }
    .axis-y,
    .axis-x {
      display: block;
    }
    .axis-x {
      text-align: end;
    }
    .axis-y::after {
      content: ' \\2193';
    }
    .axis-x::after {
      content: ' \\2192';
    }
    .cell {
      min-width: 3.25rem;
      height: 2rem;
      padding: 0 var(--space-2);
      text-align: center;
      border-radius: 3px;
      background: var(--color-surface-2);
      color: var(--color-ink);
      @include bp.coarse {
        height: var(--touch-min);
      }
    }
    .cell.na {
      color: var(--color-ink-3);
    }
    .cell.plateau {
      box-shadow: inset 0 0 0 1px var(--color-ink-3);
    }
    .cell.best {
      box-shadow: inset 0 0 0 2px var(--color-ink);
      font-weight: var(--weight-semibold);
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2) var(--space-4);
      margin: 0;
      padding: 0;
      list-style: none;
      font-size: var(--text-xs);
      color: var(--color-ink-2);
    }
    .legend li {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
    }
    .swatch {
      display: inline-block;
      width: 0.9rem;
      height: 0.9rem;
      border-radius: 2px;
      background: var(--color-surface-2);
    }
    .swatch.low {
      background: color-mix(in srgb, var(--color-loss) 45%, var(--color-surface));
    }
    .swatch.high {
      background: color-mix(in srgb, var(--color-gain) 45%, var(--color-surface));
    }
    .swatch.tuned {
      box-shadow: inset 0 0 0 2px var(--color-ink);
    }
    .swatch.hood {
      box-shadow: inset 0 0 0 1px var(--color-ink-3);
    }
  `,
})
export class ParamHeatmap {
  readonly heatmap = input.required<HeatmapView>();

  protected readonly xName = computed(() => humanize(this.heatmap().x));
  protected readonly yName = computed(() => humanize(this.heatmap().y));
  protected readonly metric = computed(() => heatmapMetricText(this.heatmap().metric));
  protected readonly caption = computed(
    () =>
      `${this.metric()} by ${this.xName()} (across) and ${this.yName()} (down). The tuned set is outlined.`,
  );
  protected readonly xLabels = computed(() => this.heatmap().x_values.map(axisText));
  protected readonly rows = computed(() => heatmapRows(this.heatmap()));
  protected readonly fixed = computed(() =>
    Object.entries(this.heatmap().fixed)
      .map(([k, v]) => `${humanize(k)} ${axisText(v)}`)
      .join(', '),
  );
  protected readonly stepText = computed(() => {
    const step = this.heatmap().plateau?.step ?? 0;
    return `${formatNumber(step * 100, { digits: 0 })}%`;
  });
  protected readonly plateauMetrics = computed(() =>
    Object.entries(this.heatmap().plateau?.metrics ?? {}).map(([key, v]) => ({
      key,
      label: metricLabel(key),
      value: formatMetric(key, v),
    })),
  );
}
