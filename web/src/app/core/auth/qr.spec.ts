import { qrModules, qrPath } from './qr';

describe('qr', () => {
  const uri = 'otpauth://totp/Stonks:ann%40example.com?secret=JBSWY3DPEHPK3PXP&issuer=Stonks';

  it('encodes text as a square grid of dark and light modules', () => {
    const grid = qrModules(uri);
    expect(grid.length).toBeGreaterThanOrEqual(21);
    for (const row of grid) expect(row).toHaveLength(grid.length);
    // Finder pattern: the top-left corner module is dark.
    expect(grid[0][0]).toBe(true);
  });

  it('draws one SVG path of unit squares for the dark modules', () => {
    const path = qrPath([
      [true, false],
      [false, true],
    ]);
    expect(path).toBe('M0 0h1v1h-1zM1 1h1v1h-1z');
  });
});
