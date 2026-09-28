import { NAV_ITEMS, groupForUrl, groupsForUrl, navItemVisible } from './nav-items';

describe('nav items', () => {
  it('finds the group of the page on screen, by the longest matching path', () => {
    expect(groupForUrl('/')).toBe('Main');
    expect(groupForUrl('/strategies/momentum_1a2b3c4d')).toBe('Main');
    expect(groupForUrl('/lab/ledger?x=1')).toBe('Advanced');
    expect(groupsForUrl('/ops/halts')).toEqual(['Advanced', 'System']);
    expect(groupForUrl('/ops/schedule')).toBe('System');
    expect(groupForUrl('/trades')).toBe('More');
    expect(groupForUrl('/nowhere')).toBeNull();
  });

  it('keeps about seven pages on top (M1)', () => {
    expect(NAV_ITEMS.filter((i) => i.group === 'Main').length).toBeLessThanOrEqual(7);
  });

  it('uses the vocabulary words: Dashboard, Trial results, Strategy review', () => {
    const labels = NAV_ITEMS.map((i) => i.label);
    expect(labels).toContain('Dashboard');
    expect(labels).toContain('Trial results');
    expect(labels).toContain('Strategy review');
    for (const old of ['Overview', 'Paper trading', 'Go live', 'Glossary']) {
      expect(labels).not.toContain(old);
    }
  });

  it('hides an item whose feature is off', () => {
    const assistant = NAV_ITEMS.find((i) => i.path === '/assistant')!;
    const viewer = { can: () => true, isAdmin: () => false };
    expect(navItemVisible(assistant, viewer)).toBe(true);
    expect(navItemVisible(assistant, { ...viewer, hasFeature: () => false })).toBe(false);
  });
});
