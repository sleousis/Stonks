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
      return {
        year,
        cells: row,
        total: total === null ? '–' : formatPercent(total, { signed: true, digits: 1 }),
        totalTone: tone(total),
      };
    });
}

/**
 * A monthly returns heatmap: one row per year, a cell per month shaded by
 * sign and size, and the year compounded at the end. Every cell carries its
 * signed percent, so the colour is never the only cue. Scrolls sideways on
 * a phone.
 *
 *   <app-monthly-returns caption="Paper return by month" [months]="sheet.monthly_returns" />
 */
@Component({
  selector: 'app-monthly-returns',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="months-scroll" tabindex="0" [attr.aria-label]="caption()">
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
            <th scope="col">Year</th>
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
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
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
        overflow: visible;
        padding: 0;
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
