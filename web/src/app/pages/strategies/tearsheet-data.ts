import type { MonthlyReturn, PnlRowView } from '../../api/models';
import { formatDate, formatPercent } from '../../core/format/format';
import type { ChartSeries } from '../../shared/chart/chart-engine';

export const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
] as const; // prettier-ignore

export interface MonthCell {
  month: string;
  value: number | null;
  text: string;
  tone: 'gain' | 'loss' | '';
}

export interface YearRow {
  year: string;
  cells: MonthCell[];
  /** Compounded over the months shown. */
  total: string;
  totalTone: 'gain' | 'loss' | '';
}

function tone(v: number | null): 'gain' | 'loss' | '' {
  if (v === null || v === 0) return '';
  return v > 0 ? 'gain' : 'loss';
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

export type MonthReturnLike = Pick<MonthlyReturn, 'month' | 'value'>;

/** The paper value as a line with its drawdown below (paper: never brass). */
export function curveSeries(curve: readonly PnlRowView[]): ChartSeries[] {
  return [
    {
      id: 'value',
      label: 'Paper value',
      kind: 'line',
      color: 'primary',
      format: 'money',
      points: curve.map((r) => ({ time: r.day, value: r.total_value })),
    },
    {
      id: 'drawdown',
      label: 'Drawdown',
      kind: 'area',
      color: 'loss',
      pane: 1,
      format: 'percent',
      points: curve.map((r) => ({ time: r.day, value: r.drawdown })),
    },
  ];
}

export function curveSummary(curve: readonly PnlRowView[]): string {
  const first = curve[0];
  const last = curve.at(-1);
  if (!first || !last) return 'No paper history yet.';
  const worst = Math.min(...curve.map((r) => r.drawdown));
  return (
    `Paper value from ${formatDate(first.day)} to ${formatDate(last.day)}: ` +
    `${formatPercent(last.cumulative_return ?? 0, { signed: true })}. ` +
    `Worst drawdown ${formatPercent(worst)}.`
  );
}
