import { holdWords } from './ticket-words';

describe('holdWords', () => {
  it('says why a ticket waits, live option orders included', () => {
    expect(holdWords('options')).toContain('every option order waits for your approval');
    expect(holdWords('hard_to_borrow')).toContain('hard to borrow');
    expect(holdWords(null)).toBeNull();
  });
});
