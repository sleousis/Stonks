import { PASSWORD_MIN, generatePassword } from './passwords';

describe('generatePassword', () => {
  it('makes four groups of five unambiguous characters, long enough to use', () => {
    const pw = generatePassword();
    expect(pw).toMatch(/^([A-Za-z2-9]{5}-){3}[A-Za-z2-9]{5}$/);
    expect(pw).not.toMatch(/[01lIoO]/);
    expect(pw.length).toBeGreaterThanOrEqual(PASSWORD_MIN);
  });

  it('skips bytes that would bias the alphabet', () => {
    const bytes = [255, 0, 1, 2, 3, 4];
    let i = 0;
    const pw = generatePassword(() => bytes[i++ % bytes.length]);
    // 255 is rejected, so the first group starts with the alphabet's first letters.
    expect(pw.slice(0, 5)).toBe('abcde');
  });
});
