import type { ChartView } from '../../api/models';
import { chartData, chartSummary, dayOf, sma, toCandles, toMarkers } from './chart-data';

function bar(day: string, close: number, open = close) {
  return {
    timestamp: `${day}T00:00:00`,
    open,
    high: Math.max(open, close) + 1,
    low: Math.min(open, close) - 1,
    close,
    adj_close: close,
    volume: 1000,
  };
}

const VIEW: ChartView = {
  ticker: 'UP.US',
  interval: '1d',
  truncated: false,
  portfolio_id: 'pf_1',
  bars: [
    bar('2026-03-02', 10),
    bar('2026-03-03', 11),
    bar('2026-03-04', 12, 12.5),
    bar('2026-03-05', 13),
    { ...bar('2026-03-06', 14), open: null },
  ],
  fills: [
    {
      filled_at: '2026-03-03T20:00:00Z',
      side: 'buy',
      quantity: 5,
      price: 11,
      order_client_id: 'o1',
      strategy_id: 'mom',
    },
    {
      filled_at: '2026-03-05T20:00:00Z',
      side: 'sell',
      quantity: -5,
      price: 13,
      order_client_id: 'o2',
      strategy_id: 'mom',
    },
    {
      filled_at: '2026-01-05T20:00:00Z',
      side: 'buy',
      quantity: 1,
      price: 9,
      order_client_id: 'o0',
      strategy_id: null,
    },
  ],
  signals: [
    { as_of: '2026-03-04', strategy_id: 'mom', kind: 'entry', strength: 0.4, reason: 'up' },
    { as_of: '2026-03-05', strategy_id: 'mom', kind: 'decrease', strength: null, reason: null },
  ],
};

describe('chart data', () => {
  it('averages over a window and waits until it is full', () => {
    expect(sma([1, 2, 3, 4], 2)).toEqual([null, 1.5, 2.5, 3.5]);
    expect(sma([5], 3)).toEqual([null]);
  });

  it('keys daily candles on the day and skips bars without a full OHLC', () => {
    const candles = toCandles(VIEW);
    expect(candles.map((c) => c.time)).toEqual([
      '2026-03-02',
      '2026-03-03',
      '2026-03-04',
      '2026-03-05',
    ]);
    expect(dayOf('2026-03-05T20:00:00Z')).toBe('2026-03-05');
    const hourly = toCandles({ ...VIEW, interval: '1h' });
    expect(hourly[0].time).toBe('2026-03-02T00:00:00');
  });

  it('marks fills B and S and signals as dots, only on the shown bars', () => {
    const shown = new Set(['2026-03-03', '2026-03-04', '2026-03-05']);
    const marks = toMarkers(VIEW.fills, VIEW.signals, shown, { fills: true, signals: true });
    expect(marks).toEqual([
      { time: '2026-03-03', kind: 'buy', text: 'B' },
      { time: '2026-03-05', kind: 'sell', text: 'S' },
      { time: '2026-03-04', kind: 'entry', text: '' },
      { time: '2026-03-05', kind: 'change', text: '' },
    ]);
    expect(toMarkers(VIEW.fills, VIEW.signals, shown, { fills: false, signals: false })).toEqual(
      [],
    );
  });

  it('draws the chosen range with the averages over the warm-up bars', () => {
    const data = chartData(VIEW, {
      bars: 2,
      averages: [2],
      fills: true,
      signals: false,
      volume: false,
    });
    expect(data.candles.map((c) => c.time)).toEqual(['2026-03-04', '2026-03-05']);
    expect(data.showVolume).toBe(false);
    expect(data.overlays).toEqual([]); // no MA 2 on offer
    const withMa = chartData(VIEW, {
      bars: 3,
      averages: [20],
      fills: true,
      signals: true,
      volume: true,
    });
    expect(withMa.overlays[0].id).toBe('ma20');
    expect(withMa.overlays[0].points).toEqual([]); // fewer than 20 bars
    expect(withMa.markers.map((m) => m.kind)).toEqual(['buy', 'sell', 'entry', 'change']);
  });

  it('sums the chart up in a sentence', () => {
    const data = chartData(VIEW, {
      bars: 10,
      averages: [],
      fills: true,
      signals: true,
      volume: true,
    });
    const text = chartSummary('UP.US', data);
    expect(text).toContain('UP.US closed at $13.00');
    expect(text).toContain('+30.00%');
    expect(text).toContain('2 of your fills and 2 strategy signals');
    expect(chartSummary('X', { ...data, candles: [] })).toBe('No prices for X in this range.');
  });
});
