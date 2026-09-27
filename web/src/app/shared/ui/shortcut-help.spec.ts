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

  it('sits on the shared sheet: Escape closes it, and hidden shortcuts are left out (UX-48)', () => {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const shortcuts = TestBed.inject(ShortcutsService);
    shortcuts.setSequences([
      { prefix: 'g', key: 's', label: 'Strategies', path: '/strategies' },
      { prefix: 'g', key: 'r', label: 'Users', path: '/admin/users', visible: () => false },
    ]);
    const fixture = TestBed.createComponent(ShortcutHelp);
    shortcuts.openHelp();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-sheet dialog')).not.toBeNull();
    expect(el.textContent).toContain('Strategies');
    expect(el.textContent).not.toContain('Users');

    el.querySelector('dialog')!.dispatchEvent(new Event('cancel', { cancelable: true }));
    fixture.detectChanges();
    expect(shortcuts.helpOpen()).toBe(false);
  });
});
