import { InjectionToken } from '@angular/core';

/**
 * The charting seam. Pages and components describe series with these types;
 * only `lightweight-chart-engine.ts` knows the charting library. To swap the
 * library, write another ChartEngine and point CHART_ENGINE at it.
 */

/** `time` is a date (`YYYY-MM-DD`, for daily data) or an ISO timestamp. */
export interface ChartPoint {
  time: string;
  value: number;
}

/**
 * Named line colours, each read from a design token. `brass` is the real
 * portfolio, `gain`/`loss` carry meaning (drawdown), `muted` a benchmark;
 * `primary`, `info`, `violet` and `ink` are categorical, for lines that only
 * need telling apart (see CATEGORICAL_LINES). `warn` is amber, too close to
 * brass, so peer lines never use it.
 */
export type ChartColor =
  'brass' | 'primary' | 'gain' | 'loss' | 'muted' | 'info' | 'warn' | 'violet' | 'ink';
export type ChartValueFormat = 'money' | 'percent' | 'number';

export interface ChartSeries {
  id: string;
  label: string;
  /** `area` fills between the line and zero (drawdown, underwater curves). */
  kind: 'line' | 'area';
  color: ChartColor;
  /** 0 = main pane; 1 = a lower pane (e.g. drawdown under equity). */
  pane?: number;
  format?: ChartValueFormat;
  /** Dashed line; pairs with a colour so more lines stay distinct. */
  dashed?: boolean;
  points: readonly ChartPoint[];
}

/**
 * Line styles for series that are peers (several strategies on one chart),
 * with no gain or loss meaning. Colours alternate hue and lightness, then
 * repeat dashed, so six lines stay apart; show at most this many.
 */
export const CATEGORICAL_LINES: readonly { color: ChartColor; dashed: boolean }[] = [
  { color: 'primary', dashed: false },
  { color: 'violet', dashed: false },
  { color: 'ink', dashed: false },
  { color: 'info', dashed: true },
  { color: 'violet', dashed: true },
  { color: 'ink', dashed: true },
];

export interface ChartTheme {
  background: string;
  text: string;
  grid: string;
  border: string;
  font: string;
  colors: Record<ChartColor, string>;
}

export interface CrosshairReadout {
  time: string | null;
  /** Series id → value at the crosshair. Null when the pointer leaves. */
  values: ReadonlyMap<string, number> | null;
}

export interface ChartHandle {
  setSeries(series: readonly ChartSeries[]): void;
  setTheme(theme: ChartTheme): void;
  onCrosshair(listener: (readout: CrosshairReadout) => void): void;
  destroy(): void;
}

/** One OHLC bar with its volume (`time` as for ChartPoint). */
export interface Candle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

/**
 * True when the price chart should fit its time scale again: the first data,
 * or other bars. Candles rebuilt for the same bars (a moving average or the
 * volume toggled) keep the trader's zoom.
 */
export function newCandleWindow(prev: readonly Candle[], next: readonly Candle[]): boolean {
  return (
    prev.length === 0 ||
    prev.length !== next.length ||
    prev[0]?.time !== next[0]?.time ||
    prev.at(-1)?.time !== next.at(-1)?.time
  );
}

/**
 * A mark on a bar: your fill (`buy` below the bar, `sell` above it) or a
 * strategy's signal (`entry` below, `exit` above, `change` on the bar).
 * `text` is one or two characters (B, S).
 */
export interface PriceMarker {
  time: string;
  kind: 'buy' | 'sell' | 'entry' | 'exit' | 'change';
  text: string;
}

export interface PriceChartData {
  candles: readonly Candle[];
  /** Lines over the candles (moving averages), pane 0. */
  overlays: readonly ChartSeries[];
  markers: readonly PriceMarker[];
  /** Volume bars along the bottom of the price pane. */
  showVolume: boolean;
}

export interface PriceReadout {
  time: string | null;
  candle: Candle | null;
  /** Overlay id -> value at the crosshair. */
  overlays: ReadonlyMap<string, number> | null;
}

export interface PriceChartHandle {
  setData(data: PriceChartData): void;
  setTheme(theme: ChartTheme): void;
  onCrosshair(listener: (readout: PriceReadout) => void): void;
  destroy(): void;
}

export interface ChartEngine {
  create(container: HTMLElement, theme: ChartTheme): ChartHandle;
  /** A candlestick chart with volume, overlays and markers. */
  createPrice(container: HTMLElement, theme: ChartTheme): PriceChartHandle;
}

/** Loads the engine lazily so the charting library stays out of the main bundle. */
export const CHART_ENGINE = new InjectionToken<() => Promise<ChartEngine>>('CHART_ENGINE', {
  providedIn: 'root',
  factory: () => () => import('./lightweight-chart-engine').then((m) => m.lightweightChartEngine),
});
