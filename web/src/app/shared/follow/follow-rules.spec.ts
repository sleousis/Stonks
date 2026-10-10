import { sub } from '../../../testing/home-fixtures';
import { blockerDetail, gatedModesLocked, lockedModesLine, paperProgress } from './follow-rules';

describe('follow rules (FR-03, PO-02)', () => {
  it('opens the gated modes once the gate passes', () => {
    expect(gatedModesLocked(sub())).toBe(false);
    expect(lockedModesLine(sub())).toBeNull();
    expect(blockerDetail(sub())).toBeNull();
  });

  it('says both modes need a broker portfolio and the paper days', () => {
    const waiting = sub({
      mode: 'notify',
      portfolio_id: null,
      paper_days_completed: 0,
      auto_blockers: ['auto mode needs a portfolio', '0 of 20 paper trading days completed'],
    });
    expect(lockedModesLine(waiting)).toBe(
      'More modes unlock with a broker portfolio after 20 paper days.',
    );
    // Alerts only collects no paper days and the portfolio is in the line.
    expect(blockerDetail(waiting)).toBeNull();
  });

  it('a paper follow says how far it has come, never repeating the server line', () => {
    const paper = sub({
      paper_days_completed: 12,
      auto_blockers: [
        'auto mode needs a broker portfolio',
        '12 of 20 paper trading days completed without a risk breach',
      ],
    });
    expect(lockedModesLine(paper)).toBe(
      'More modes unlock with a broker portfolio after 20 paper days.',
    );
    expect(blockerDetail(paper)).toBe('Paper days: 12 of 20.');
  });

  it('names only what is missing', () => {
    expect(lockedModesLine(sub({ paper_days_completed: 4 }))).toBe(
      'More modes unlock after 20 paper days.',
    );
    expect(lockedModesLine(sub({ auto_blockers: ['auto mode needs a broker portfolio'] }))).toBe(
      'More modes unlock with a broker portfolio.',
    );
  });

  it('shows other blockers as sentences in trader words (UX-09)', () => {
    const s = sub({ auto_blockers: ['the strategy is not active', 'the portfolio is paused'] });
    expect(lockedModesLine(s)).toBe('More modes are closed for now.');
    expect(blockerDetail(s)).toBe('The strategy is not approved yet. The portfolio is paused.');
  });

  it('counts paper days in the vocabulary words (M8)', () => {
    expect(paperProgress(sub({ paper_days_completed: 4 }))).toBe('Paper days: 4 of 20.');
    expect(paperProgress(sub({ paper_days_completed: 20 }))).toBeNull();
  });
});
