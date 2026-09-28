import type { CompareSeriesView, CompareView } from '../../api/models';
import { formatDate, formatNumber, formatPercent } from '../../core/format/format';
import { CATEGORICAL_LINES, type ChartSeries } from '../../shared/chart/chart-engine';

/** Tickers you can add next to the chart's own (six lines in all). */
export const MAX_COMPARED = CATEGORICAL_LINES.length - 1;

/** Windows the rolling Sharpe can use, in daily bars. */
export const SHARPE_WINDOWS = [
  { bars: 63, label: '3M' },
  { bars: 126, label: '6M' },
  { bars: 252, label: '1Y' },
] as const;
export type SharpeWindow = (typeof SHARPE_WINDOWS)[number]['bars'];

/** Tickers typed in a box or read from `?vs=`: split on commas or spaces, upper case, once each. */
export function parseTickers(text: string | null | undefined): string[] {
  const out: string[] = [];
  for (const raw of (text ?? '').split(/[\s,]+/)) {
    const t = raw.trim().toUpperCase();
    if (t && t.length <= 40 && !out.includes(t)) out.push(t);
  }
  return out;
}

/** `compared` plus `add`, without the chart's own ticker, at most MAX_COMPARED. */
export function addTickers(own: string, compared: readonly string[], add: readonly string[]) {
  const out = [...compared];
  for (const t of add) {
    if (t !== own && !out.includes(t) && out.length < MAX_COMPARED) out.push(t);
  }
  return out;
}

/** Every ticker rebased to 100, one categorical line each, the chart's own first. */
export function compareSeries(view: CompareView): ChartSeries[] {
  return view.series.map((s, i) => {
    const style = CATEGORICAL_LINES[i % CATEGORICAL_LINES.length];
    return {
      id: `cmp-${s.ticker}`,
      label: s.ticker,
      kind: 'line',
      color: style.color,
      dashed: style.dashed,
      format: 'number',
      points: s.points.map((p) => ({ time: p.time, value: p.value })),
    };
  });
}

/** The chart's own ticker: rolling Sharpe on top, drawdown below. */
export function performanceSeries(series: CompareSeriesView, window: number): ChartSeries[] {
  return [
    {
      id: 'sharpe',
      label: `Rolling Sharpe (${window} days)`,
      kind: 'line',
      color: 'primary',
      format: 'number',
      points: series.rolling_sharpe.map((p) => ({ time: p.time, value: p.value })),
    },
    {
      id: 'drawdown',
      label: 'Drawdown',
      kind: 'area',
      color: 'loss',
      pane: 1,
      format: 'percent',
      points: series.drawdown.map((p) => ({ time: p.time, value: p.value })),
    },
  ];
}

const signed = (v: number | null | undefined) =>
  v === null || v === undefined ? 'n/a' : formatPercent(v, { signed: true, digits: 1 });

/** One or two sentences with the takeaway, for screen readers. */
export function compareSummary(view: CompareView): string {
  if (!view.series.length || !view.start) return 'No prices to compare in this range.';
  const parts = view.series.map((s) => `${s.ticker} ${signed(s.total_return)}`);
  const missing = view.missing.length ? ` No prices for ${view.missing.join(', ')}.` : '';
  return `Rebased to 100 on ${formatDate(view.start)}: ${parts.join(', ')}.${missing}`;
}

export function performanceSummary(series: CompareSeriesView, window: number): string {
  const sharpe =
    series.sharpe === null || series.sharpe === undefined
      ? `not enough days yet for a ${window}-day Sharpe`
      : `rolling Sharpe ${formatNumber(series.sharpe, { digits: 2 })} over ${window} days`;
  const worst =
    series.max_drawdown === null || series.max_drawdown === undefined
      ? ''
      : `, worst drawdown ${formatPercent(series.max_drawdown, { digits: 1 })}`;
  return `${series.ticker}: ${sharpe}${worst}.`;
}

/** A row of the figures table under the compare chart. */
export interface CompareRow {
  ticker: string;
  change: string;
  worst: string;
  sharpe: string;
  tone: 'gain' | 'loss' | '';
}

export function compareRows(view: CompareView): CompareRow[] {
  return view.series.map((s) => ({
    ticker: s.ticker,
    change: signed(s.total_return),
    worst:
      s.max_drawdown === null || s.max_drawdown === undefined
        ? 'n/a'
        : formatPercent(s.max_drawdown, { digits: 1 }),
    sharpe:
      s.sharpe === null || s.sharpe === undefined ? 'n/a' : formatNumber(s.sharpe, { digits: 2 }),
    tone: !s.total_return ? '' : s.total_return > 0 ? 'gain' : 'loss',
  }));
}
