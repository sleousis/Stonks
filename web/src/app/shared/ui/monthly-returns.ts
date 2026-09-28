import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { MonthlyReturn } from '../../api/models';
import { formatPercent } from '../../core/format/format';

export const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
] as const; // prettier-ignore

export type MonthReturnLike = Pick<MonthlyReturn, 'month' | 'value'>;
export type Tone = 'gain' | 'loss' | '';
/** How strong a cell's shade is: 1 under 2%, 2 under 5%, 3 from 5% on. */
export type Strength = 0 | 1 | 2 | 3;

export interface MonthCell {
  month: string;
  value: number | null;
  text: string;
  tone: Tone;
  strength: Strength;
}

export interface YearRow {
  year: string;
  cells: MonthCell[];
  /**
   * The months from the first to the last one with a return, for the
   * stacked phone view: never the empty months around them.
   */
  span: MonthCell[];
  /** Compounded over the months shown. */
  total: string;
  totalTone: Tone;
}

function tone(v: number | null): Tone {
  if (v === null || v === 0) return '';
  return v > 0 ? 'gain' : 'loss';
}

export function strength(v: number | null): Strength {
  if (v === null || v === 0) return 0;
  const size = Math.abs(v);
  if (size < 0.02) return 1;
  return size < 0.05 ? 2 : 3;
}

/** Monthly returns as one row per year, twelve cells each, newest year first. */
export function yearRows(months: readonly MonthReturnLike[]): YearRow[] {
  const byYear = new Map<string, Map<number, number | null>>();
  for (const m of months) {
    const [year, month] = m.month.split('-');
    if (!byYear.has(year)) byYear.set(year, new Map());
    byYear.get(year)!.set(Number(month) - 1, m.value ?? null);
  }
  return [...byYear.entries()]
    .sort(([a], [b]) => (a < b ? 1 : -1))
    .map(([year, cells]) => {
      let growth = 1;
      let any = false;
      const row = MONTHS.map((label, i) => {
        const v = cells.has(i) ? (cells.get(i) ?? null) : null;
        if (v !== null) {
          growth *= 1 + v;
          any = true;
        }
        return {
          month: label,
          value: v,
          text: v === null ? '' : formatPercent(v, { signed: true, digits: 1 }),
          tone: tone(v),
          strength: strength(v),
        };
      });
      const total = any ? growth - 1 : null;
      const first = row.findIndex((c) => c.value !== null);
      const last = row.length - 1 - [...row].reverse().findIndex((c) => c.value !== null);
      return {
        year,
        cells: row,
        span: first < 0 ? [] : row.slice(first, last + 1),
        total: total === null ? '–' : formatPercent(total, { signed: true, digits: 1 }),
        totalTone: tone(total),
      };
    });
}

/**
 * A monthly returns heatmap: one row per year, a cell per month shaded by
 * sign and size, and the year compounded at the end. Every cell carries its
 * signed percent, so the colour is never the only cue. Where the grid does
 * not fit (a phone, a narrow panel) it stacks by year instead: each year's
 * total, then only the months that have a return, so the latest month is
 * never off screen.
 *
 *   <app-monthly-returns caption="Paper return by month" [months]="sheet.monthly_returns" />
 */
@Component({
  selector: 'app-monthly-returns',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="months-scroll" tabindex="0" role="region" [attr.aria-label]="caption()">
      <table class="months">
        <caption class="visually-hidden">
          {{
            caption()
          }}
        </caption>
        <thead>
          <tr>
            <th scope="col">Year</th>
            @for (m of monthNames; track m) {
              <th scope="col">{{ m }}</th>
            }
            <th scope="col">Year total</th>
          </tr>
        </thead>
        <tbody>
          @for (y of rows(); track y.year) {
            <tr>
              <th scope="row" class="num">{{ y.year }}</th>
              @for (c of y.cells; track c.month) {
                <td class="num cell" [attr.data-tone]="c.tone" [attr.data-strength]="c.strength">
                  {{ c.text }}
                </td>
              }
              <td class="num total" [attr.data-tone]="y.totalTone">{{ y.total }}</td>
            </tr>
          }
        </tbody>
      </table>
    </div>
    <div class="stacked" role="region" [attr.aria-label]="caption()">
      @for (y of rows(); track y.year) {
        <section class="year" [attr.aria-label]="y.year">
          <p class="year-head">
            <span class="num year-name">{{ y.year }}</span>
            <span class="year-total">
              Year total
              <span class="num total" [attr.data-tone]="y.totalTone">{{ y.total }}</span>
            </span>
          </p>
          @if (y.span.length) {
            <dl class="year-months">
              @for (c of y.span; track c.month) {
                <div class="cell" [attr.data-tone]="c.tone" [attr.data-strength]="c.strength">
                  <dt>{{ c.month }}</dt>
                  <dd class="num">{{ c.text || '–' }}</dd>
                </div>
              }
            </dl>
          }
        </section>
      }
    </div>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
      container-type: inline-size;
    }
    .stacked {
      display: none;
      gap: var(--space-3);
      padding: 0 var(--space-4) var(--space-3);
    }
    /* The grid needs about 44rem; narrower, it stacks by year (never hides a month). */
    @container (max-width: 46rem) {
      .months-scroll {
        display: none;
      }
      .stacked {
        display: grid;
      }
    }
    .year-head {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      gap: var(--space-1) var(--space-3);
      margin-bottom: var(--space-2);
      font-size: var(--text-sm);
    }
    .year-name {
      font-weight: var(--weight-semibold);
    }
    .year-total {
      color: var(--color-ink-3);
    }
    .year-total .total {
      padding: 0 var(--space-1);
      border-radius: var(--radius-sm);
    }
    .year-months {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(5.5rem, 1fr));
      gap: 2px;
      margin: 0;
    }
    .year-months .cell {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: var(--space-2);
      padding: var(--space-2);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
      font-size: var(--text-sm);
    }
    .year-months dt {
      color: var(--color-ink-2);
    }
    .year-months dd {
      margin: 0;
    }
    .months-scroll {
      overflow-x: auto;
      padding: 0 var(--space-4) var(--space-3);
    }
    .months {
      width: 100%;
      min-width: 44rem;
      border-collapse: collapse;
      font-size: var(--text-sm);
    }
    th {
      padding: var(--space-1) var(--space-2);
      color: var(--color-ink-3);
      font-weight: var(--weight-medium);
      text-align: right;
    }
    th[scope='row'] {
      text-align: left;
      color: var(--color-ink);
    }
    td {
      padding: var(--space-1) var(--space-2);
      border: 1px solid var(--color-surface);
      text-align: right;
      white-space: nowrap;
    }
    .cell[data-tone='gain'],
    .total[data-tone='gain'] {
      background: var(--color-gain-soft);
      color: var(--color-gain);
    }
    .cell[data-tone='loss'],
    .total[data-tone='loss'] {
      background: var(--color-loss-soft);
      color: var(--color-loss);
    }
    .cell[data-tone='gain'][data-strength='2'] {
      background: color-mix(in srgb, var(--color-gain) 22%, var(--color-gain-soft));
    }
    .cell[data-tone='gain'][data-strength='3'] {
      background: color-mix(in srgb, var(--color-gain) 40%, var(--color-gain-soft));
      color: var(--color-ink);
    }
    .cell[data-tone='loss'][data-strength='2'] {
      background: color-mix(in srgb, var(--color-loss) 22%, var(--color-loss-soft));
    }
    .cell[data-tone='loss'][data-strength='3'] {
      background: color-mix(in srgb, var(--color-loss) 40%, var(--color-loss-soft));
      color: var(--color-ink);
    }
    .total {
      font-weight: var(--weight-semibold);
    }
    @media print {
      .months-scroll {
        display: block;
        overflow: visible;
        padding: 0;
      }
      .stacked {
        display: none;
      }
      .months {
        min-width: 0;
      }
    }
  `,
})
export class MonthlyReturns {
  readonly months = input.required<readonly MonthReturnLike[]>();
  /** What the table shows, for screen readers ("Paper return by month"). */
  readonly caption = input.required<string>();

  protected readonly monthNames = MONTHS;
  protected readonly rows = computed(() => yearRows(this.months()));
}
