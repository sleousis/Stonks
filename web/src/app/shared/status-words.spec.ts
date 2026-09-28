import { RUN_WORDS, runWords } from './status-words';

describe('status words', () => {
  it('gives a trading run the one word per state of the vocabulary (M5)', () => {
    expect(RUN_WORDS.ok).toEqual({ status: 'ok', label: 'Done' });
    expect(RUN_WORDS.partial.label).toBe('Partly done');
    expect(RUN_WORDS.error).toEqual({ status: 'failed', label: 'Failed' });
    expect(runWords('running').label).toBe('Running');
  });

  it('never shows a lower-case system key for a status it does not know', () => {
    expect(runWords('skipped')).toEqual({ status: 'skipped', label: 'Skipped' });
  });
});
