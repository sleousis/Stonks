import {
  NAV_ITEMS,
  WORKSPACES,
  itemForUrl,
  navItemVisible,
  workspaceForUrl,
  workspacesForUrl,
} from './nav-items';

describe('nav items', () => {
  it('finds the workspace of the page on screen, by the longest matching path', () => {
    expect(workspaceForUrl('/')).toBe('Trade');
    expect(workspaceForUrl('/orders/fills')).toBe('Trade');
    expect(workspaceForUrl('/journal/calendar')).toBe('Trade');
    expect(workspaceForUrl('/strategies/momentum_1a2b3c4d')).toBe('Research');
    expect(workspaceForUrl('/lab/ledger?x=1')).toBe('Research');
    expect(workspaceForUrl('/studio')).toBe('Research');
    expect(workspaceForUrl('/insights')).toBe('Portfolio');
    expect(workspacesForUrl('/ops/halts')).toEqual(['Portfolio', 'System']);
    expect(workspaceForUrl('/ops/schedule')).toBe('System');
    expect(workspaceForUrl('/notifications/settings')).toBe('Account');
    expect(workspaceForUrl('/nowhere')).toBeNull();
  });

  it('finds the item of a sub-page', () => {
    expect(itemForUrl('/orders/ticks')?.label).toBe('Orders');
    expect(itemForUrl('/')?.label).toBe('Today');
    expect(itemForUrl('/nowhere')).toBeNull();
  });

  it('keeps every workspace to a handful of tabs', () => {
    for (const ws of WORKSPACES) {
      const items = NAV_ITEMS.filter((i) => i.workspace === ws.name);
      expect(items.length).toBeGreaterThan(0);
      expect(items.length).toBeLessThanOrEqual(10);
    }
  });

  it('keeps every page path once per audience', () => {
    const seen = new Set<string>();
    for (const item of NAV_ITEMS) {
      const audience = item.adminOnly ? 'admin' : item.notForAdmins ? 'trader' : 'all';
      const key = `${item.path} ${audience}`;
      expect(seen.has(key)).toBe(false);
      seen.add(key);
    }
  });

  it('uses the vocabulary words: Dashboard, Trial results, Strategy review', () => {
    const labels = NAV_ITEMS.map((i) => i.label);
    expect(labels).toContain('Dashboard');
    expect(labels).toContain('Trial results');
    expect(labels).toContain('Strategy review');
    expect(labels).toContain('Journal');
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
