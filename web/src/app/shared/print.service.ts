import { DOCUMENT } from '@angular/common';
import { Injectable, inject } from '@angular/core';

import { ThemeService } from '../core/theme/theme.service';

/** Frames to wait so charts redraw in the light theme before printing. */
const REDRAW_FRAMES = 2;

/**
 * "Download PDF" through the browser's print dialog, where the person picks
 * "Save as PDF". No PDF library runs on the server: the page's print
 * stylesheet (`styles.scss`) keeps only the content, on white. A dark theme
 * switches to light for the print and back afterwards, so charts print in
 * ink colours too.
 */
@Injectable({ providedIn: 'root' })
export class PrintService {
  private readonly doc = inject(DOCUMENT);
  private readonly theme = inject(ThemeService);

  async print(): Promise<void> {
    const win = this.doc.defaultView;
    if (!win) return;
    const mode = this.theme.mode();
    const dark = this.theme.resolved() === 'dark';
    if (dark) {
      this.theme.setMode('light');
      for (let i = 0; i < REDRAW_FRAMES; i++) await nextFrame(win);
    }
    try {
      win.print();
    } finally {
      if (dark) this.theme.setMode(mode);
    }
  }
}

function nextFrame(win: Window): Promise<void> {
  return new Promise((resolve) => {
    if (typeof win.requestAnimationFrame === 'function') win.requestAnimationFrame(() => resolve());
    else setTimeout(resolve, 16);
  });
}
