import { strategyKindName } from './strategy-format';

describe('strategyKindName', () => {
  it('turns a class path into a readable name', () => {
    expect(strategyKindName('stonks.strategies.momentum.MomentumStrategy')).toBe(
      'Momentum strategy',
    );
    expect(strategyKindName('BuyAndHold')).toBe('Buy and hold');
    expect(strategyKindName('x.RSIMeanReversion')).toBe('RSI mean reversion');
    expect(strategyKindName('x.TSMOM')).toBe('TSMOM');
  });
});
