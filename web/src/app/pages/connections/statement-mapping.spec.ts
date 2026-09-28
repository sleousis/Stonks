import { cleanMapping, textToList, textToTypes, typesToText } from './statement-mapping';

describe('statement mapping helpers', () => {
  it('writes and reads type values', () => {
    expect(typesToText({ BUY: 'trade', DIV: 'dividend' })).toBe('BUY=trade, DIV=dividend');
    expect(typesToText(null)).toBe('');
    const { types, bad } = textToTypes('buy=Trade, DIV = dividend,, WIRE=money');
    expect(types).toEqual({ BUY: 'trade', DIV: 'dividend' });
    expect(bad).toEqual(['WIRE=money']);
  });

  it('reads a list of sale values', () => {
    expect(textToList('sell, Sold,\n')).toEqual(['SELL', 'SOLD']);
  });

  it('drops blank columns before sending', () => {
    const mapping = cleanMapping({
      date: 'Date',
      type: '',
      symbol: null,
      kind: 'trade',
      types: {},
      sell_values: [],
      default_currency: 'USD',
    });
    expect(mapping).toEqual({
      date: 'Date',
      kind: 'trade',
      types: {},
      sell_values: [],
      default_currency: 'USD',
    });
  });
});
