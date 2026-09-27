import { argValue, toolLabel } from './tool-labels';

describe('tool labels', () => {
  it('names known tools in words and falls back to the id in words', () => {
    expect(toolLabel('get_portfolio')).toBe('Read your portfolio');
    expect(toolLabel('engage_kill_switch')).toBe('Stop trading');
    expect(toolLabel('get_new_thing')).toBe('Get new thing');
  });

  it('clips long values and writes booleans as words', () => {
    expect(argValue(true)).toBe('yes');
    expect(argValue(null)).toBe('none');
    expect(argValue('x'.repeat(200), 10)).toHaveLength(10);
  });
});
