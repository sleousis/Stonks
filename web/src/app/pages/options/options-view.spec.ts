import { CHAIN, PAYOFF } from './options-test-fixtures';
import {
  backtestErrors,
  boundText,
  buildBacktestRequest,
  chainColumns,
  checkLabel,
  costText,
  defaultBacktestForm,
  legText,
  paramLabel,
  payoffGeometry,
  splitTickers,
  structureLabel,
} from './options-view';

describe('options view helpers', () => {
  it('builds chain columns for each side, the strike first', () => {
    const both = chainColumns('both');
    expect(both[0].key).toBe('strike');
    expect(both[0].mobile).toBe('title');
    expect(both.map((c) => c.label)).toContain('Call bid');
    expect(both.map((c) => c.label)).toContain('Put vega');
    expect(both.find((c) => c.key === 'call:gamma')!.mobile).toBe('hide');
    expect(both.find((c) => c.key === 'put:delta')!.mobile).toBe('show');

    const calls = chainColumns('calls');
    expect(calls.map((c) => c.label)).toEqual([
      'Strike',
      'Bid',
      'Ask',
      'IV',
      'Delta',
      'Gamma',
      'Theta',
      'Vega',
      'Open interest',
    ]);
    const row = CHAIN.rows[0];
    const iv = calls.find((c) => c.key === 'call:iv')!;
    expect(iv.display!(row)).toBe('25.0%');
    expect(iv.value!(row)).toBe(0.25);
    const puts = chainColumns('puts');
    const putDelta = puts.find((c) => c.key === 'put:delta')!;
    expect(putDelta.display!(row)).toBe('-0.44');
    expect(putDelta.value!(CHAIN.rows[1])).toBeNull();
  });

  it('names structures, parameters and checks plainly', () => {
    expect(structureLabel('bull_call_spread')).toBe('Bull call spread');
    expect(paramLabel('dte')).toBe('Days to expiry');
    expect(paramLabel('wing_delta')).toBe('Wing delta');
    expect(paramLabel('other_thing')).toBe('Other thing');
    expect(checkLabel('fill_stress')).toBe('Wider fills');
    expect(checkLabel('new_check')).toBe('New check');
  });

  it('describes legs, bounds and cost', () => {
    expect(legText(PAYOFF.legs[0])).toBe('Buy 1 call 100, 2026-04-17');
    expect(legText(PAYOFF.legs[1])).toBe('Sell 1 call 110, 2026-04-17');
    expect(legText({ instrument: 'AAPL.US', kind: 'shares', quantity: 100, price: 100 })).toBe(
      'Hold 100 shares of AAPL.US',
    );
    expect(boundText(null)).toBe('Unlimited');
    expect(boundText(800)).toBe('$800.00');
    expect(costText(200)).toBe('Debit $200.00');
    expect(costText(-150)).toBe('Credit $150.00');
  });

  it('scales the payoff so gain and loss meet on the zero line', () => {
    const g = payoffGeometry(PAYOFF.points, 100)!;
    expect(g.line.startsWith('M64.0,')).toBe(true);
    expect(g.line.split('L')).toHaveLength(PAYOFF.points.length);
    expect(g.gain.endsWith('Z')).toBe(true);
    expect(g.zeroY).toBeGreaterThan(12);
    expect(g.zeroY).toBeLessThan(252);
    expect(g.spotX).toBeCloseTo(64 + (30 / 60) * 560, 5);
    expect(g.xTicks.length).toBeGreaterThan(2);
    expect(g.yTicks.some((t) => Math.abs(t.y - g.zeroY) < 1e-6)).toBe(true);
    expect(payoffGeometry(PAYOFF.points, 500)!.spotX).toBeNull();
    expect(payoffGeometry([{ spot: 1, profit: 0 }], 1)).toBeNull();
    const flat = payoffGeometry(
      [
        { spot: 1, profit: 0 },
        { spot: 2, profit: 0 },
      ],
      null,
    )!;
    expect(flat.line).toContain('L');
  });

  it('checks the backtest form in words and builds the request', () => {
    const empty = backtestErrors({ ...defaultBacktestForm(), cash: null });
    expect(Object.keys(empty).sort()).toEqual(['cash', 'end', 'start', 'strategy', 'underlyings']);
    const f = {
      strategy: 'cash_secured_put',
      underlyings: 'aapl.us, MSFT.US aapl.us',
      start: '2026-03-01',
      end: '2026-01-01',
      cash: 5000,
      validation: false,
    };
    expect(backtestErrors(f)).toEqual({ end: 'The last day comes after the first.' });
    expect(splitTickers(f.underlyings)).toEqual(['AAPL.US', 'MSFT.US']);
    const many = Array.from({ length: 11 }, (_, i) => `T${i}.US`).join(',');
    expect(backtestErrors({ ...f, end: '2026-04-01', underlyings: many })['underlyings']).toContain(
      'At most 10',
    );
    expect(buildBacktestRequest({ ...f, end: '2026-04-01' }, { delta: 0.3 })).toEqual({
      strategy: 'cash_secured_put',
      underlyings: ['AAPL.US', 'MSFT.US'],
      start: '2026-03-01',
      end: '2026-04-01',
      cash: 5000,
      params: { delta: 0.3 },
      validation: false,
    });
  });
});
