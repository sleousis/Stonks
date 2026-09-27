import { killStopsText, killTicket } from './kill-ticket';

describe('killTicket', () => {
  it('lists scope, what stops, what still goes out and the reason', () => {
    const t = killTicket({
      scopeText: 'Portfolio Main',
      buysOnly: true,
      reason: ' Market is wild ',
      live: true,
    });
    expect(t.kind).toBe('Kill switch');
    expect(t.live).toBe(true);
    expect(t.lines).toEqual([
      { label: 'Scope', value: 'Portfolio Main' },
      { label: 'Stops', value: 'New buys' },
      { label: 'Still goes out', value: 'Sells and exits' },
      { label: 'Reason', value: 'Market is wild' },
    ]);
  });

  it('says all new orders stop when not buys only', () => {
    expect(killStopsText(false)).toBe('All new orders');
    const t = killTicket({
      scopeText: 'Every portfolio',
      buysOnly: false,
      reason: '',
      live: false,
    });
    expect(t.lines[1].value).toBe('All new orders');
    expect(t.lines[2].value).toBe('Nothing new');
  });
});
