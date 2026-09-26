import type { Provider } from '@angular/core';

import {
  CHART_ENGINE,
  type ChartEngine,
  type ChartHandle,
  type ChartSeries,
} from '../app/shared/chart/chart-engine';

/** Records what a TimeSeriesChart would draw; jsdom has no canvas. */
export class FakeChartEngine implements ChartEngine {
  readonly series: (readonly ChartSeries[])[] = [];

  create(): ChartHandle {
    return {
      setSeries: (s) => this.series.push(s),
      setTheme: () => undefined,
      onCrosshair: () => undefined,
      destroy: () => undefined,
    };
  }

  get last(): readonly ChartSeries[] | undefined {
    return this.series.at(-1);
  }
}

export function provideFakeChart(engine = new FakeChartEngine()): Provider {
  return { provide: CHART_ENGINE, useValue: () => Promise.resolve(engine) };
}
