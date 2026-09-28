import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { MeView } from '../api/models';
import { provideApi } from '../api/provide-api';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { CommandRegistry, commandScore } from '../core/commands/command-registry';
import { ShortcutsService } from '../core/commands/shortcuts.service';
import { StopTradingService } from '../core/halts/stop-trading.service';
import { ADMIN, TRADER } from '../../testing/auth-fixtures';
import { registerShellCommands } from './shell-commands';

describe('registerShellCommands', () => {
  const me = signal<MeView | null>(TRADER);
  let registry: CommandRegistry;
  let shortcuts: ShortcutsService;

  beforeEach(() => {
    me.set(TRADER);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: SessionService,
          useValue: {
            me,
            canRead: () => true,
            isAdmin: () => me()?.role === 'admin',
            can: (p: Permission) => allowed(me(), p),
            csrfToken: () => null,
          },
        },
      ],
    });
    TestBed.runInInjectionContext(() => registerShellCommands());
    registry = TestBed.inject(CommandRegistry);
    shortcuts = TestBed.inject(ShortcutsService);
  });

  const labels = () => registry.available().map((c) => c.label);
  const best = (q: string) =>
    [...registry.available()]
      .map((c) => ({ c, s: commandScore(q, c.label, c.keywords) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)[0]?.c.label;

  it("'run' finds Trading runs; a non-admin's palette has no /admin/users (UX-41)", () => {
    expect(best('run')).toBe('Trading runs');
    const ids = registry.available().map((c) => c.id);
    expect(ids).not.toContain('page./admin/users');
    expect(ids).not.toContain('page./ops/schedule');
    for (const label of labels()) {
      expect(label).not.toMatch(/\bticks?\b|\bingest|\bshadow\b/i);
    }
  });

  it('uses the nav words: Trial results, Strategy review, Trade costs', () => {
    expect(labels()).toEqual(
      expect.arrayContaining(['Trial results', 'Strategy review', 'Trade costs', 'Strategies']),
    );
  });

  it('follows the signed-in user: admin pages and g r appear for an admin, reactively', () => {
    const gr = () => shortcuts.allSequences.find((s) => s.prefix === 'g' && s.key === 'r');
    expect(gr()).toBeUndefined();
    expect(labels()).not.toContain('Try a dry run');
    me.set(ADMIN);
    expect(registry.available().map((c) => c.id)).toContain('page./admin/users');
    expect(gr()?.path).toBe('/admin/users');
    expect(labels()).toContain('Try a dry run');
  });

  it('a hidden g sequence does nothing', () => {
    const press = (key: string, at: number) =>
      shortcuts.handle(new KeyboardEvent('keydown', { key, cancelable: true }), at);
    press('g', 1000);
    expect(press('r', 1100)).toBe(false);
  });

  it('keeps g t for Trade costs, now a tab under Orders', () => {
    const gt = shortcuts.allSequences.find((s) => s.prefix === 'g' && s.key === 't');
    expect(gt?.path).toBe('/trades');
  });

  it('Stop trading opens the kill switch sheet', async () => {
    const stop = registry.available().find((c) => c.label === 'Stop trading')!;
    await stop.run();
    expect(TestBed.inject(StopTradingService).open()).toBe(true);
  });
});
