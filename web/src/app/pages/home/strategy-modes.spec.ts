import { sub } from '../../../testing/home-fixtures';
import { autoBlockedReason } from './strategy-modes';

describe('autoBlockedReason', () => {
  it('is null once the gate passes', () => {
    expect(autoBlockedReason(sub())).toBeNull();
  });

  it('counts paper days first, without repeating the server line', () => {
    expect(
      autoBlockedReason(
        sub({
          paper_days_completed: 12,
          auto_blockers: ['12 of 20 paper trading days', 'the portfolio is not a broker portfolio'],
        }),
      ),
    ).toBe(
      'Approve each trade and Auto unlock after 20 paper trading days. 12 of 20 done. The portfolio is not a broker portfolio.',
    );
  });

  it('shows other blockers as sentences', () => {
    expect(autoBlockedReason(sub({ auto_blockers: ['the portfolio is paused'] }))).toBe(
      'The portfolio is paused.',
    );
  });

  it('says approved instead of the status key (UX-09)', () => {
    expect(autoBlockedReason(sub({ auto_blockers: ['the strategy is not active'] }))).toBe(
      'The strategy is not approved yet.',
    );
  });
});
