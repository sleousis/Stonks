import type { PnlRowView } from '../../api/models';
import { formatDate, formatPercent } from '../../core/format/format';
import type { ChartSeries } from '../../shared/chart/chart-engine';

export {
  MONTHS,
  type MonthCell,
  type MonthReturnLike,
  type YearRow,
  yearRows,
} from '../../shared/ui/monthly-returns';

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
