import { journalView } from './journal.page';
import {
  exitLabel,
  formatHolding,
  formatProfitFactor,
  formatR,
  formatRate,
  groupLabel,
  plural,
  monthGrid,
  parseLabels,
  planLabel,
  shiftMonth,
  sleeveLabel,
} from './journal-format';

describe('journal format', () => {
  it('writes R multiples with a sign, and n/a without a stop', () => {
    expect(formatR(2)).toBe('+2.0R');
    expect(formatR(-1)).toBe('-1.0R');
    expect(formatR(null)).toBe('n/a');
    expect(formatR(-0.04)).toBe('0.0R');
    expect(formatR(0.04)).toBe('0.0R');
    expect(formatR(0.06)).toBe('+0.1R');
  });

  it('writes rates with one decimal and profit factors with two', () => {
    expect(formatRate(0.272727)).toBe('27.3%');
    expect(formatRate(null)).toBe('n/a');
    expect(formatProfitFactor(2.8466)).toBe('2.85');
    expect(formatProfitFactor(null)).toBe('n/a');
  });

  it('counts in the singular and the plural', () => {
    expect(plural(1, 'trade')).toBe('1 trade');
    expect(plural(3, 'trade')).toBe('3 trades');
  });

  it('says holding time in plain words', () => {
    expect(formatHolding(0.01)).toBe('Under an hour');
    expect(formatHolding(0.25)).toBe('6 hours');
    expect(formatHolding(1)).toBe('1 day');
    expect(formatHolding(3.04)).toBe('3 days');
    expect(formatHolding(2.46)).toBe('2.5 days');
  });

  it('names exits, sleeves, plans and groups', () => {
    expect(exitLabel('stop', false)).toBe('Stop');
    expect(exitLabel('signal', true)).toBe('Still open');
    expect(exitLabel(null, false)).toBe('Not recorded');
    expect(sleeveLabel('manual')).toBe('By hand');
    expect(sleeveLabel('momentum')).toBe('Momentum');
    expect(planLabel(false)).toBe('Broke the plan');
    expect(planLabel(null)).toBe('Not said');
    expect(groupLabel('broke', 'plan')).toBe('Broke the plan');
    expect(groupLabel('manual', 'sleeve')).toBe('By hand');
    expect(groupLabel('open', 'exit_trigger')).toBe('Still open');
  });

  it('parses comma separated labels', () => {
    expect(parseLabels(' Gap  Up, earnings,,gap up ')).toEqual(['gap up', 'earnings']);
  });

  it('lays a month out Monday first with weekly totals', () => {
    // March 2026 starts on a Sunday: six pad cells before the 1st.
    const weeks = monthGrid('2026-03', [
      { key: '2026-03-02', pnl: 10, trades: 2 },
      { key: '2026-03-04', pnl: -4, trades: 1 },
    ]);
    expect(weeks[0].cells.slice(0, 6).every((c) => c.iso === null)).toBe(true);
    expect(weeks[0].cells[6].iso).toBe('2026-03-01');
    expect(weeks[0].total).toBeNull();
    expect(weeks[1].total).toBe(6);
    expect(weeks[1].trades).toBe(3);
    expect(weeks.every((w) => w.cells.length === 7)).toBe(true);
  });

  it('reads the view from the address, falling back to Trades', () => {
    expect(journalView('playbooks')).toBe('playbooks');
    expect(journalView('nonsense')).toBe('trades');
    expect(journalView(undefined)).toBe('trades');
  });

  it('steps months across years', () => {
    expect(shiftMonth('2026-01', -1)).toBe('2025-12');
    expect(shiftMonth('2026-12', 1)).toBe('2027-01');
  });
});
