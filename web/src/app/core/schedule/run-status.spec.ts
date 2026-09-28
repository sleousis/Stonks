import { CHECK_WORDS, runWord } from './run-status';

describe('runWord', () => {
  it('says Done for every code that means a run finished well', () => {
    for (const code of ['ok', 'succeeded', 'success', 'completed', 'OK']) {
      expect(runWord(code)).toBe('Done');
    }
  });

  it('says Partly done and Failed for the other endings', () => {
    expect(runWord('partial')).toBe('Partly done');
    expect(runWord('failed')).toBe('Failed');
    expect(runWord('error')).toBe('Failed');
  });

  it('names runs still going, skipped or never run', () => {
    expect(runWord('running')).toBe('Running');
    expect(runWord('queued')).toBe('Waiting');
    expect(runWord('skipped')).toBe('Skipped');
    expect(runWord(null)).toBe('Not run yet');
  });

  it('writes an unknown code in words, never as a raw id', () => {
    expect(runWord('timed_out')).toBe('Timed out');
  });
});

describe('CHECK_WORDS', () => {
  it('uses the three check words of the vocabulary', () => {
    expect(Object.values(CHECK_WORDS)).toEqual(['Passed', 'Failed', 'Not enough data yet']);
  });
});
