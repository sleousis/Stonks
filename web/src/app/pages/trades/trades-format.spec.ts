import {
  bps1,
  formatBps,
  humanize,
  portfolioLive,
  portfolioName,
  triggerLabel,
} from './trades-format';

describe('trades-format', () => {
  it('formats basis points with a sign, n/a while unknown', () => {
    expect(formatBps(12.34)).toBe('+12.3 bps');
    expect(formatBps(-3)).toBe('-3 bps');
    expect(formatBps(null)).toBe('n/a');
    expect(bps1(12.36)).toBe(12.4);
    expect(bps1(undefined)).toBeNull();
  });

  it('names triggers and keys in plain words', () => {
    expect(triggerLabel('exit_no_pick')).toBe('Exit with no new pick');
    expect(triggerLabel(null)).toBe('Not recorded');
    expect(humanize('target_weight')).toBe('Target weight');
  });

  it('looks portfolios up in the picker list, never showing a raw id', () => {
    const options = [
      { id: 'pf_1', name: 'Main', mode: 'live' as const, is_default: true },
      { id: 'pf_2', name: 'Practice', mode: 'paper' as const },
    ];
    expect(portfolioName('pf_2', options)).toBe('Practice');
    expect(portfolioName('pf_9', options)).toBeNull();
    expect(portfolioLive('pf_2', options)).toBe(false);
    expect(portfolioLive(null, options)).toBe(true);
    expect(portfolioLive('pf_1', [])).toBeNull();
  });
});
