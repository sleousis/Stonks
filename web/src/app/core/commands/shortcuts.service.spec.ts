import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { CommandRegistry, commandScore, matchScore } from './command-registry';
import { SHORTCUTS_STORAGE_KEY, ShortcutsService } from './shortcuts.service';

function key(k: string, init: KeyboardEventInit = {}, target?: EventTarget): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key: k, cancelable: true, ...init });
  if (target) Object.defineProperty(event, 'target', { value: target });
  return event;
}

describe('matchScore', () => {
  it('ranks prefix over word start over substring over letters in order', () => {
    const prefix = matchScore('mom', 'momentum-v3');
    const word = matchScore('v3', 'momentum-v3');
    const inner = matchScore('ment', 'momentum-v3');
    const letters = matchScore('mv3', 'momentum-v3');
    expect(prefix).toBeGreaterThan(word);
    expect(word).toBeGreaterThan(inner);
    expect(inner).toBeGreaterThan(letters);
    expect(letters).toBeGreaterThan(0);
    expect(matchScore('xyz', 'momentum-v3')).toBe(0);
    // Scattered letters must start at a word start.
    expect(matchScore('sha', 'Dashboard')).toBe(0);
    expect(matchScore('sha', 'Shadow')).toBeGreaterThan(0);
    expect(matchScore('', 'anything')).toBeGreaterThan(0);
  });

  it('finds commands by keyword', () => {
    expect(commandScore('dry', 'Run a dry-run tick')).toBeGreaterThan(0);
    expect(commandScore('simulate', 'New backtest', ['lab', 'simulate'])).toBeGreaterThan(0);
    expect(commandScore('zzz', 'New backtest', ['lab'])).toBe(0);
  });
});

describe('CommandRegistry', () => {
  it('registers, replaces by id and removes commands', () => {
    const registry = TestBed.inject(CommandRegistry);
    const run = vi.fn();
    const remove = registry.register([{ id: 'a', label: 'A', group: 'Actions', run }]);
    registry.register([{ id: 'b', label: 'B', group: 'Pages', run }]);
    expect(registry.commands().map((c) => c.id)).toEqual(['a', 'b']);
    remove();
    expect(registry.commands().map((c) => c.id)).toEqual(['b']);
  });
});

describe('ShortcutsService', () => {
  let svc: ShortcutsService;
  let router: Router;

  beforeEach(() => {
    localStorage.clear();
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    svc = TestBed.inject(ShortcutsService);
    router = TestBed.inject(Router);
    svc.setSequences([
      { prefix: 'g', key: 'd', label: 'Dashboard', path: '/' },
      { prefix: 'g', key: 'l', label: 'Lab', path: '/lab' },
      { prefix: 'n', key: 'b', label: 'New backtest', commandId: 'new-backtest' },
    ]);
  });
  afterEach(() => localStorage.clear());

  it('toggles the palette with Ctrl+K and Cmd+K, even while typing', () => {
    const input = document.createElement('input');
    const first = key('k', { ctrlKey: true }, input);
    expect(svc.handle(first)).toBe(true);
    expect(first.defaultPrevented).toBe(true);
    expect(svc.paletteOpen()).toBe(true);
    svc.handle(key('K', { metaKey: true }));
    expect(svc.paletteOpen()).toBe(false);
  });

  it('opens the palette with / and the cheat sheet with ?', () => {
    svc.handle(key('/'));
    expect(svc.paletteOpen()).toBe(true);
    svc.handle(key('?'));
    expect(svc.helpOpen()).toBe(true);
    expect(svc.paletteOpen()).toBe(false);
  });

  it('navigates on g then a key within the time window', () => {
    const navigate = vi.spyOn(router, 'navigateByUrl').mockResolvedValue(true);
    svc.handle(key('g'), 1000);
    expect(svc.handle(key('l'), 1500)).toBe(true);
    expect(navigate).toHaveBeenCalledWith('/lab');

    navigate.mockClear();
    svc.handle(key('g'), 1000);
    svc.handle(key('l'), 5000);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('runs a registered command on n then a key', () => {
    const run = vi.fn();
    TestBed.inject(CommandRegistry).register([
      { id: 'new-backtest', label: 'New backtest', group: 'Actions', run },
    ]);
    svc.handle(key('n'), 0);
    svc.handle(key('b'), 100);
    expect(run).toHaveBeenCalledOnce();
  });

  it('ignores single keys while typing in a field', () => {
    const input = document.createElement('input');
    expect(svc.handle(key('/', {}, input))).toBe(false);
    expect(svc.paletteOpen()).toBe(false);
  });

  it('can turn single-key shortcuts off (remembered), leaving Ctrl+K on', () => {
    svc.setSingleKeys(false);
    expect(localStorage.getItem(SHORTCUTS_STORAGE_KEY)).toBe('off');
    expect(svc.handle(key('?'))).toBe(false);
    expect(svc.helpOpen()).toBe(false);
    expect(svc.handle(key('k', { ctrlKey: true }))).toBe(true);
    svc.setSingleKeys(true);
    expect(localStorage.getItem(SHORTCUTS_STORAGE_KEY)).toBeNull();
  });

  it('leaves other modifier combinations to the browser', () => {
    const event = key('d', { altKey: true });
    expect(svc.handle(event)).toBe(false);
    expect(event.defaultPrevented).toBe(false);
  });
});
