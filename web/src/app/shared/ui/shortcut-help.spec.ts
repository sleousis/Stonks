import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { ShortcutsService } from '../../core/commands/shortcuts.service';
import { ShortcutHelp } from './shortcut-help';

describe('ShortcutHelp', () => {
  it('lists every shortcut and switches single-key shortcuts off', async () => {
    localStorage.clear();
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const shortcuts = TestBed.inject(ShortcutsService);
    shortcuts.setSequences([
      { prefix: 'g', key: 'd', label: 'Dashboard', path: '/' },
      { prefix: 'n', key: 'b', label: 'New backtest', commandId: 'x' },
    ]);
    const fixture = TestBed.createComponent(ShortcutHelp);
    shortcuts.openHelp();
    fixture.detectChanges();
    await fixture.whenStable();
    const el: HTMLElement = fixture.nativeElement;

    const text = el.textContent ?? '';
    expect(text).toContain('Search and run commands');
    expect(text).toContain('Dashboard');
    expect(text).toContain('New backtest');
    expect(el.querySelector('dialog')!.getAttribute('aria-labelledby')).toBe('shortcuts-title');

    const toggle = el.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(toggle.checked).toBe(true);
    toggle.click();
    expect(shortcuts.singleKeys()).toBe(false);
    localStorage.clear();
  });
});
