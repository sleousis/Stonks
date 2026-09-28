import { STATUS_FILTERS } from './strategy-format';

describe('STATUS_FILTERS', () => {
  it('reads All, Approved, On trial, Retired (B1)', () => {
    expect(STATUS_FILTERS.map((f) => f.label)).toEqual(['All', 'Approved', 'On trial', 'Retired']);
  });
});
