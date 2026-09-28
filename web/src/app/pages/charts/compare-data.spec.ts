import type { CompareSeriesView, CompareView } from '../../api/models';
import {
  MAX_COMPARED,
  addTickers,
  compareRows,
  compareSeries,
  compareSummary,
  parseTickers,
  performanceSeries,
  performanceSummary,
} from './compare-data';

function series(ticker: string, over: Partial<CompareSeriesView> = {}): CompareSeriesView {
  return {
    ticker,
    points: [
      { time: '2025-01-02', value: 100 },
      { time: '2025-01-03', value: 110 },
    ],
    drawdown: [
      { time: '2025-01-02', value: 0 },
      { time: '2025-01-03', value: -0.05 },
    ],
    rolling_sharpe: [{ time: '2025-01-03', value: 1.2 }],
    total_return: 0.1,
    max_drawdown: -0.05,
    sharpe: 1.2,
    periods_per_year: 252,
    ...over,
  };
}

const VIEW: CompareView = {
  start: '2025-01-02',
  end: '2025-01-03',
  window: 63,
  missing: ['NOPE.US'],
  series: [series('UP.US'), series('DOWN.US', { total_return: -0.2, sharpe: null })],
};

describe('compare data', () => {
  it('reads tickers from a box or the address, once each, upper case', () => {
    expect(parseTickers(' msft.us, spy.us  msft.us,,')).toEqual(['MSFT.US', 'SPY.US']);
    expect(parseTickers(undefined)).toEqual([]);
    expect(parseTickers('x'.repeat(41))).toEqual([]);
  });

  it('adds tickers without the chart own one and stops at six lines', () => {
    expect(addTickers('UP.US', ['A'], ['UP.US', 'A', 'B'])).toEqual(['A', 'B']);
    const full = addTickers('UP.US', [], ['A', 'B', 'C', 'D', 'E', 'F', 'G']);
    expect(full.length).toBe(MAX_COMPARED);
  });

  it('draws one categorical line per ticker, never brass', () => {
    const lines = compareSeries(VIEW);
    expect(lines.map((l) => l.id)).toEqual(['cmp-UP.US', 'cmp-DOWN.US']);
    expect(lines.map((l) => l.color)).toEqual(['primary', 'violet']);
    expect(lines.every((l) => l.color !== 'brass' && l.format === 'number')).toBe(true);
  });

  it('puts the rolling Sharpe on top and the drawdown below', () => {
    const [sharpe, drawdown] = performanceSeries(VIEW.series[0], 63);
    expect(sharpe).toMatchObject({ id: 'sharpe', kind: 'line', label: 'Rolling Sharpe (63 days)' });
    expect(drawdown).toMatchObject({ kind: 'area', pane: 1, color: 'loss', format: 'percent' });
  });

  it('writes the takeaway for screen readers', () => {
    expect(compareSummary(VIEW)).toContain('UP.US +10.0%, DOWN.US -20.0%');
    expect(compareSummary(VIEW)).toContain('No prices for NOPE.US');
    expect(compareSummary({ ...VIEW, series: [], start: null })).toBe(
      'No prices to compare in this range.',
    );
    expect(performanceSummary(VIEW.series[0], 63)).toBe(
      'UP.US: rolling Sharpe 1.2 over 63 days, worst drawdown -5.0%.',
    );
    expect(performanceSummary(VIEW.series[1], 126)).toContain('not enough days yet');
  });

  it('lists each ticker change, worst drawdown and Sharpe with a tone', () => {
    const rows = compareRows(VIEW);
    expect(rows[0]).toEqual({
      ticker: 'UP.US',
      change: '+10.0%',
      worst: '-5.0%',
      sharpe: '1.2',
      tone: 'gain',
    });
    expect(rows[1]).toMatchObject({ sharpe: 'n/a', tone: 'loss' });
  });
});
