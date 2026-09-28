import { strategyDisplayName, strategyKindName } from './strategy-names';

describe('strategyDisplayName (UX-27)', () => {
  it('prefers the draft name', () => {
    expect(strategyDisplayName('my_breakout_1a2b3c4d', { draftName: 'My breakout' })).toBe(
      'My breakout',
    );
  });

  it("shows a starter strategy's own title", () => {
    const starter = { title: 'Buy and hold the market', summary: 'Holds one ticker.' };
    expect(strategyDisplayName('starter_buy_and_hold', { starter })).toBe(
      'Buy and hold the market',
    );
    expect(strategyDisplayName('starter_buy_and_hold', { starter: null })).toBe(
      'starter_buy_and_hold',
    );
  });

  it('turns a registered id into a name with a short suffix', () => {
    expect(strategyDisplayName('stocks_on_the_move_3fa9c21b')).toBe('Stocks on the move 3fa9');
    expect(strategyDisplayName('momentum_0a1b2c3d')).toBe('Momentum 0a1b');
  });

  it('keeps an id that has no generated suffix', () => {
    expect(strategyDisplayName('momentum-v3')).toBe('momentum-v3');
    expect(strategyDisplayName('')).toBe('');
  });
});

describe('strategyKindName', () => {
  it('turns a class path into a readable name', () => {
    expect(strategyKindName('stonks.strategies.momentum.MomentumStrategy')).toBe(
      'Momentum strategy',
    );
    expect(strategyKindName('BuyAndHold')).toBe('Buy and hold');
    expect(strategyKindName('x.RSIMeanReversion')).toBe('RSI mean reversion');
    // The registry stores entry-point paths: module, a colon, then the class.
    expect(strategyKindName('stonks.strategies.examples.buy_and_hold:BuyAndHold')).toBe(
      'Buy and hold',
    );
    expect(strategyKindName('x.TSMOM')).toBe('TSMOM');
  });
});
