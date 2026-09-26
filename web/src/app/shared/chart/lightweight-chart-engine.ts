// The only file that imports the charting library (TradingView Lightweight
// Charts, Apache-2.0). Everything else talks to the ChartEngine interface.
import {
  BaselineSeries,
  type IChartApi,
  type ISeriesApi,
  LineSeries,
  type LineData,
  LineStyle,
  type MouseEventParams,
  type SeriesType,
  type Time,
  type UTCTimestamp,
  createChart,
} from 'lightweight-charts';

import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import type {
  ChartEngine,
  ChartHandle,
  ChartPoint,
  ChartSeries,
  ChartTheme,
  ChartValueFormat,
  CrosshairReadout,
} from './chart-engine';

const LOWER_PANE_STRETCH = 0.35;

function toTime(time: string): Time {
  if (/^\d{4}-\d{2}-\d{2}$/.test(time)) return time;
  return Math.floor(Date.parse(time) / 1000) as UTCTimestamp;
}

function fromTime(time: Time | undefined): string | null {
  if (time === undefined) return null;
  if (typeof time === 'string') return time;
  if (typeof time === 'number') return new Date(time * 1000).toISOString();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${time.year}-${pad(time.month)}-${pad(time.day)}`;
}

function formatter(format: ChartValueFormat | undefined): (v: number) => string {
  switch (format) {
    case 'money':
      return (v) => formatMoney(v, { compact: Math.abs(v) >= 100_000 });
    case 'percent':
      return (v) => formatPercent(v, { digits: 1 });
    default:
      return (v) => formatNumber(v, { compact: Math.abs(v) >= 100_000, digits: 2 });
  }
}

function toData(points: readonly ChartPoint[]): LineData<Time>[] {
  // The library requires strictly ascending, unique times.
  const byTime = new Map<string | number, LineData<Time>>();
  for (const p of points) {
    if (!Number.isFinite(p.value)) continue;
    const time = toTime(p.time);
    byTime.set(time as string | number, { time, value: p.value });
  }
  return [...byTime.values()].sort((a, b) =>
    (a.time as string | number) < (b.time as string | number) ? -1 : 1,
  );
}

class LightweightChart implements ChartHandle {
  private readonly chart: IChartApi;
  private readonly series = new Map<string, ISeriesApi<SeriesType>>();
  private specs: readonly ChartSeries[] = [];
  private theme: ChartTheme;
  private listener: ((r: CrosshairReadout) => void) | null = null;
  private readonly resizeObserver: ResizeObserver | null = null;
  private frame = 0;

  constructor(container: HTMLElement, theme: ChartTheme) {
    this.theme = theme;
    this.chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight,
      handleScroll: {
        vertTouchDrag: false,
        horzTouchDrag: true,
        mouseWheel: false,
        pressedMouseMove: true,
      },
      handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { mode: 0 },
      layout: { attributionLogo: false, panes: { enableResize: false } },
    });
    this.applyTheme();
    this.chart.subscribeCrosshairMove((p) => this.emitCrosshair(p));
    // Our own observer (instead of `autoSize`) resizes on the next frame, which
    // avoids "ResizeObserver loop" errors when the layout reflows.
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => {
        cancelAnimationFrame(this.frame);
        this.frame = requestAnimationFrame(() =>
          this.chart.resize(container.clientWidth, container.clientHeight),
        );
      });
      this.resizeObserver.observe(container);
    }
  }

  setSeries(specs: readonly ChartSeries[]): void {
    this.specs = specs;
    const wanted = new Set(specs.map((s) => s.id));
    for (const [id, api] of this.series) {
      if (!wanted.has(id)) {
        this.chart.removeSeries(api);
        this.series.delete(id);
      }
    }
    for (const spec of specs) {
      let api = this.series.get(spec.id);
      if (!api) {
        api =
          spec.kind === 'area'
            ? this.chart.addSeries(
                BaselineSeries,
                { baseValue: { type: 'price', price: 0 } },
                spec.pane ?? 0,
              )
            : this.chart.addSeries(LineSeries, {}, spec.pane ?? 0);
        this.series.set(spec.id, api);
      }
      api.applyOptions({
        ...this.seriesStyle(spec),
        priceFormat: { type: 'custom', formatter: formatter(spec.format), minMove: 0.0001 },
        title: '',
        lastValueVisible: true,
        priceLineVisible: false,
      });
      api.setData(toData(spec.points));
    }
    const panes = this.chart.panes();
    if (panes.length > 1) {
      panes[0].setStretchFactor(1 - LOWER_PANE_STRETCH);
      for (const pane of panes.slice(1)) pane.setStretchFactor(LOWER_PANE_STRETCH);
    }
    this.chart.timeScale().fitContent();
  }

  setTheme(theme: ChartTheme): void {
    this.theme = theme;
    this.applyTheme();
    for (const spec of this.specs) {
      this.series.get(spec.id)?.applyOptions(this.seriesStyle(spec));
    }
  }

  onCrosshair(listener: (r: CrosshairReadout) => void): void {
    this.listener = listener;
  }

  destroy(): void {
    this.listener = null;
    this.resizeObserver?.disconnect();
    cancelAnimationFrame(this.frame);
    this.chart.remove();
  }

  private applyTheme(): void {
    const t = this.theme;
    this.chart.applyOptions({
      layout: {
        background: { color: t.background },
        textColor: t.text,
        fontFamily: t.font,
        fontSize: 11,
        panes: { separatorColor: t.border, separatorHoverColor: t.border },
      },
      grid: { vertLines: { visible: false }, horzLines: { color: t.grid } },
    });
  }

  private seriesStyle(spec: ChartSeries) {
    const color = this.theme.colors[spec.color];
    if (spec.kind === 'area') {
      // Filled between the line and zero (drawdown hangs below the zero line).
      return {
        topLineColor: color,
        topFillColor1: withAlpha(color, 0.35),
        topFillColor2: withAlpha(color, 0.05),
        bottomLineColor: color,
        bottomFillColor1: withAlpha(color, 0.05),
        bottomFillColor2: withAlpha(color, 0.35),
        lineWidth: 1 as const,
      };
    }
    return {
      color,
      lineWidth: 2 as const,
      lineStyle: spec.dashed ? LineStyle.Dashed : LineStyle.Solid,
    };
  }

  private emitCrosshair(p: MouseEventParams<Time>): void {
    if (!this.listener) return;
    if (!p.time || !p.point) {
      this.listener({ time: null, values: null });
      return;
    }
    const values = new Map<string, number>();
    for (const [id, api] of this.series) {
      const d = p.seriesData.get(api) as { value?: number } | undefined;
      if (d && typeof d.value === 'number') values.set(id, d.value);
    }
    this.listener({ time: fromTime(p.time), values });
  }
}

/** `#rrggbb` → `rgba(r, g, b, a)`; other colour strings pass through. */
function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export const lightweightChartEngine: ChartEngine = {
  create: (container, theme) => new LightweightChart(container, theme),
};
