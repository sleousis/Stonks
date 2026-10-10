import { INSIGHTS, POLICY } from './insights.fixtures';
import { limitRows, noLimitText } from './limit-rows';

function byKey(rows: ReturnType<typeof limitRows>) {
  return Object.fromEntries(rows.map((r) => [r.key, r]));
}

describe('limitRows', () => {
  it('puts each measure next to its limit with a status', () => {
    const rows = byKey(limitRows(INSIGHTS, POLICY));
    expect(rows['top_weight']).toMatchObject({ now: '50.0%', limit: '50.0%', status: 'near' });
    expect(rows['positions']).toMatchObject({ now: '3', limit: '10', status: 'ok' });
    expect(rows['class:crypto']).toMatchObject({ now: '10.0%', limit: '5.0%', status: 'over' });
    expect(rows['gross']).toMatchObject({ now: '80.0%', limit: '150.0%', status: 'ok' });
    expect(rows['volatility']).toMatchObject({ now: '22.0%', limit: '25.0%', status: 'near' });
    expect(rows['drawdown']).toMatchObject({ now: '5.0%', limit: '20.0%', status: 'ok' });
    expect(rows['sector']).toMatchObject({ now: '60.0%', limit: 'No limit', status: 'none' });
    expect(rows['violations']).toBeUndefined();
    expect(rows['gross'].usage).toBeCloseTo(0.8 / 1.5);
  });

  it('treats a 100% holding cap as no limit', () => {
    const rows = byKey(limitRows(INSIGHTS, { ...POLICY, max_weight_per_ticker: 1 }));
    expect(rows['top_weight'].status).toBe('none');
  });

  it('folds the measures without a limit into one sentence', () => {
    const rows = limitRows(INSIGHTS, { ...POLICY, rules: {} });
    expect(noLimitText(rows)).toBe('No limit on sector, exposure, volatility or drawdown.');
    expect(noLimitText(rows.filter((r) => r.key === 'positions'))).toBe('');
  });

  it('reads a net exposure range and flags a book below it', () => {
    const policy = { ...POLICY, rules: { net_exposure: { min_net: 0.9, max_net: 1.2 } } };
    const net = byKey(limitRows(INSIGHTS, policy))['net'];
    expect(net.limit).toBe('90.0% to 120.0%');
    expect(net.status).toBe('over');
  });

  it('shows no readings while the inputs load', () => {
    const rows = limitRows(null, null);
    expect(rows.every((r) => r.usage === null)).toBe(true);
    expect(byKey(rows)['positions'].limit).toBe('No limit');
  });
});
