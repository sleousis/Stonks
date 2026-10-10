import { LAB_SECTIONS } from './lab-nav';

describe('LAB_SECTIONS', () => {
  it('keeps "trial" for On trial only and names the signal check plainly', () => {
    const labels = LAB_SECTIONS.map((s) => s.label);
    expect(labels).toContain('Lab history');
    expect(labels).toContain('Signal check');
    expect(labels.join(' ')).not.toMatch(/trial|IC\b/i);
  });
});
