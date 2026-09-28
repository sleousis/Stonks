import { DOCUMENT } from '@angular/common';
import { TestBed } from '@angular/core/testing';

import { ThemeService } from '../core/theme/theme.service';
import { PrintService } from './print.service';

describe('PrintService', () => {
  let print: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    const win = TestBed.inject(DOCUMENT).defaultView!;
    print = vi.fn();
    win.print = print as unknown as () => void;
  });

  afterEach(() => TestBed.inject(ThemeService).setMode('system'));

  it('opens the print dialog in the light theme', async () => {
    const theme = TestBed.inject(ThemeService);
    theme.setMode('light');
    await TestBed.inject(PrintService).print();
    expect(print).toHaveBeenCalledTimes(1);
    expect(theme.mode()).toBe('light');
  });

  it('prints a dark page in light colours, then puts the dark theme back', async () => {
    const theme = TestBed.inject(ThemeService);
    theme.setMode('dark');
    let during: string | null = null;
    print.mockImplementation(() => {
      during = theme.resolved();
    });
    await TestBed.inject(PrintService).print();
    expect(during).toBe('light');
    expect(theme.mode()).toBe('dark');
  });
});
