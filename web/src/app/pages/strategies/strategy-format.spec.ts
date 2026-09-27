import { STATUS_FILTERS } from './strategy-format';

describe('STATUS_FILTERS', () => {
  it('reads All, Live, Paper trading, Stopped (UX-09)', () => {
    expect(STATUS_FILTERS.map((f) => f.label)).toEqual(['All', 'Live', 'Paper trading', 'Stopped']);
  });
});
