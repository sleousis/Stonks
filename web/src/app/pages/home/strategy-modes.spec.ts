import { sub } from '../../../testing/home-fixtures';
import { autoBlockedReason, paperProgress, unlockRule } from './strategy-modes';

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
    ).toBe('Paper days: 12 of 20. The portfolio is not a broker portfolio.');
  });

  it('says the unlock rule once, in the vocabulary words (M8)', () => {
    expect(unlockRule(20)).toBe('Approve each trade and Automatic unlock after 20 paper days.');
    expect(paperProgress(sub({ paper_days_completed: 4 }))).toBe('Paper days: 4 of 20.');
    expect(paperProgress(sub({ paper_days_completed: 20 }))).toBeNull();
  });

  it('names the mode as the vocabulary does', () => {
    expect(autoBlockedReason(sub({ auto_blockers: ['auto mode needs a broker portfolio'] }))).toBe(
      'Automatic needs a broker portfolio.',
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
