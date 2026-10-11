import { FAILED_RUN_TEXT, jobErrorText } from './job-error';

describe('jobErrorText', () => {
  it('turns a raw exception into one plain sentence', () => {
    expect(jobErrorText("TypeError: cannot pickle '_thread.lock' object")).toEqual({
      text: FAILED_RUN_TEXT,
      details: null,
    });
    expect(jobErrorText('duckdb.IOException: lock held').text).toBe(FAILED_RUN_TEXT);
  });

  it('shows a message written for people as it is', () => {
    expect(jobErrorText('Enter at least one ticker.')).toEqual({
      text: 'Enter at least one ticker.',
      details: null,
    });
  });

  it('keeps the raw details only when the server sent them (admins)', () => {
    expect(
      jobErrorText('The job stopped because of a problem on our side.', 'TypeError: boom'),
    ).toEqual({
      text: 'The job stopped because of a problem on our side.',
      details: 'TypeError: boom',
    });
  });
});
