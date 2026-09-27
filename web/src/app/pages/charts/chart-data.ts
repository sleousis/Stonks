import type { ChartFillView, ChartSignalView, ChartView } from '../../api/models';
import { formatDate, formatMoney, formatPercent } from '../../core/format/format';
import type {
  Candle,
  ChartColor,
  ChartSeries,
  PriceChartData,
  PriceMarker,
} from '../../shared/chart/chart-engine';

/** Ranges the chart offers, in daily bars. */
export const RANGES = [
  { id: '3m', label: '3M', bars: 63 },
  { id: '6m', label: '6M', bars: 126 },
  { id: '1y', label: '1Y', bars: 252 },
  { id: '3y', label: '3Y', bars: 756 },
  { id: 'all', label: 'All', bars: 4_800 },
] as const;
export type RangeId = (typeof RANGES)[number]['id'];

/** Moving averages the chart can draw, each with its own line. */
export const AVERAGES: readonly { length: number; color: ChartColor; dashed: boolean }[] = [
  { length: 20, color: 'info', dashed: false },
  { length: 50, color: 'violet', dashed: false },
  { length: 200, color: 'ink', dashed: true },
];
/** Extra bars read so the longest average starts at the left edge. */
export const WARMUP = 199;

export interface ChartOptions {
  bars: number;
  averages: readonly number[];
  fills: boolean;
  signals: boolean;
  volume: boolean;
}

/** `YYYY-MM-DD` of a bar or fill time (daily charts key on the day). */
export function dayOf(iso: string): string {
  return iso.slice(0, 10);
}

/** Simple moving average; `null` until `length` values are in. */
export function sma(values: readonly number[], length: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= length) sum -= values[i - length];
    out.push(i >= length - 1 ? sum / length : null);
  }
  return out;
}

/** Bars with a full OHLC as candles, oldest first. Daily bars key on the day. */
export function toCandles(view: ChartView): Candle[] {
  const daily = !/[mh]$/.test(view.interval) || view.interval === '1mo';
  const out: Candle[] = [];
  for (const b of view.bars) {
    if (b.open === null || b.high === null || b.low === null || b.close === null) continue;
    out.push({
      time: daily ? dayOf(b.timestamp) : b.timestamp,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.volume,
    });
  }
  return out;
}

function kindOf(signal: ChartSignalView): PriceMarker['kind'] {
  if (signal.kind === 'entry') return 'entry';
  if (signal.kind === 'exit') return 'exit';
  return 'change';
}

/** Fills and signals as markers on the shown bars only. */
export function toMarkers(
  fills: readonly ChartFillView[],
  signals: readonly ChartSignalView[],
  shown: ReadonlySet<string>,
  options: Pick<ChartOptions, 'fills' | 'signals'>,
): PriceMarker[] {
  const marks: PriceMarker[] = [];
  if (options.fills) {
    for (const f of fills) {
      const time = dayOf(f.filled_at);
      if (!shown.has(time) || !f.side) continue;
      marks.push({
        time,
        kind: f.side === 'sell' ? 'sell' : 'buy',
        text: f.side === 'sell' ? 'S' : 'B',
      });
    }
  }
  if (options.signals) {
    for (const s of signals) {
      if (shown.has(s.as_of)) marks.push({ time: s.as_of, kind: kindOf(s), text: '' });
    }
  }
  return marks;
}

/** What the price chart draws for the chosen range and toggles. */
export function chartData(view: ChartView, options: ChartOptions): PriceChartData {
  const all = toCandles(view);
  const closes = all.map((c) => c.close);
  const start = Math.max(0, all.length - options.bars);
  const candles = all.slice(start);
  const overlays: ChartSeries[] = [];
  for (const avg of AVERAGES) {
    if (!options.averages.includes(avg.length)) continue;
    const values = sma(closes, avg.length);
    const points = [];
    for (let i = start; i < all.length; i++) {
      const v = values[i];
      if (v !== null) points.push({ time: all[i].time, value: v });
    }
    overlays.push({
      id: `ma${avg.length}`,
      label: `MA ${avg.length}`,
      kind: 'line',
      color: avg.color,
      dashed: avg.dashed,
      format: 'money',
      points,
    });
  }
  const shown = new Set(candles.map((c) => c.time));
  return {
    candles,
    overlays,
    markers: toMarkers(view.fills, view.signals, shown, options),
    showVolume: options.volume,
  };
}

/** One or two sentences for screen readers and the chart's caption. */
export function chartSummary(ticker: string, data: PriceChartData): string {
  const first = data.candles[0];
  const last = data.candles.at(-1);
  if (!first || !last) return `No prices for ${ticker} in this range.`;
  const change = first.close > 0 ? last.close / first.close - 1 : 0;
  const fills = data.markers.filter((m) => m.kind === 'buy' || m.kind === 'sell').length;
  const signals = data.markers.length - fills;
  const marks: string[] = [];
  if (fills) marks.push(`${fills} of your fills`);
  if (signals) marks.push(`${signals} strategy signals`);
  const tail = marks.length ? ` Marked: ${marks.join(' and ')}.` : '';
  return (
    `${ticker} closed at ${formatMoney(last.close)} on ${formatDate(last.time)}, ` +
    `${formatPercent(change, { signed: true })} since ${formatDate(first.time)}.${tail}`
  );
}
