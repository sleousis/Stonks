import { formatMetric, metricLabel } from './metrics';

describe('formatMetric', () => {
  it('shows held-out and stitched returns and drawdowns as percentages', () => {
    expect(formatMetric('max_drawdown_oos', -0.171)).toBe(formatMetric('max_drawdown', -0.171));
    expect(formatMetric('max_drawdown_oos', -0.171)).toContain('%');
    expect(formatMetric('final_return_oos', 0.25)).toContain('%');
    expect(formatMetric('cagr_oos_stitched', 0.1)).toContain('%');
  });

  it('keeps plain numbers plain', () => {
    expect(formatMetric('sharpe_oos', 1.234)).not.toContain('%');
    expect(formatMetric('n_trades', 5)).toBe('5');
  });
});

describe('metricLabel', () => {
  it('labels the walk-forward and stability figures in words', () => {
    for (const key of ['sharpe_std', 'n_windows', 'fold0_oos_score', 'fold2_cagr_is']) {
      expect(metricLabel(key)).not.toMatch(/_|\bN\b|std|oos|\bis\b/i);
    }
  });
});
