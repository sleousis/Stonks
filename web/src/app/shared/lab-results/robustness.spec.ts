import { robustnessWords } from './robustness';

describe('robustnessWords', () => {
  it("names a run's status in the Lab's words", () => {
    expect(robustnessWords('survived')).toEqual({ status: 'passed', label: 'Held up' });
    expect(robustnessWords('did_not_survive')).toEqual({
      status: 'failed',
      label: 'Did not hold up',
    });
    expect(robustnessWords('running').label).toBe('Running');
    expect(robustnessWords('stopped').label).toBe('Stopped');
    expect(robustnessWords('error').label).toBe('Failed');
    expect(robustnessWords('odd_value').label).toBe('odd value');
  });
});
