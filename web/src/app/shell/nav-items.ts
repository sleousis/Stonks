import type { Permission } from '../core/auth/permissions';
import type { Feature } from '../core/features/feature-flags.service';

/**
 * Where an item sits:
 * - `Main`: the trader's pages, on top for everyone signed in, about seven;
 * - `More`: markets, trial results and trade costs, one fold down;
 * - `Advanced`: the build tools and the strategy review;
 * - `System`: the operator pages, admins only;
 * - `Account`: the menu by the user's name, pinned to the bottom of the rail.
 */
export type NavGroup = 'Main' | 'More' | 'Advanced' | 'System' | 'Account';

/** One page in the navigation. `key` is the "g then key" keyboard shortcut. */
export interface NavItem {
  path: string;
  label: string;
  key?: string;
  group: NavGroup;
  /** Shown only to users with this permission (the page is for doing, not reading). */
  permission?: Permission;
  /** Shown to admins only (the route or the API still checks). */
  adminOnly?: boolean;
  /** Hidden from admins, who reach the same page from the System group. */
  notForAdmins?: boolean;
  /** Hidden while the server has this feature off (F42). */
  feature?: Feature;
  /** Extra words that find it in the command palette. */
  keywords?: readonly string[];
}

/**
 * The navigation, in order (M1). Seven trader pages on top. The rest sits in
 * folding groups, so the rail fits a laptop screen and the account menu,
 * pinned below it, never scrolls away. Words follow docs/design/vocabulary.md.
 */
export const NAV_ITEMS: readonly NavItem[] = [
  { path: '/', label: 'Today', key: 'm', group: 'Main', keywords: ['home'] },
  { path: '/strategies', label: 'Strategies', key: 's', group: 'Main', keywords: ['follow'] },
  { path: '/orders', label: 'Orders', key: 'o', group: 'Main', keywords: ['trades', 'fills'] },
  {
    path: '/tickets',
    label: 'Approvals',
    group: 'Main',
    permission: 'portfolio.trade',
    keywords: ['tickets', 'approve', 'order tickets'],
  },
  {
    path: '/insights',
    label: 'Insights',
    key: 'e',
    group: 'Main',
    keywords: ['portfolio', 'risk', 'value'],
  },
  { path: '/charts', label: 'Charts', key: 'z', group: 'Main', keywords: ['price'] },
  { path: '/notifications', label: 'Notifications', key: 'n', group: 'Main', keywords: ['alerts'] },

  {
    path: '/going-live',
    label: 'Going live',
    group: 'More',
    keywords: ['real money', 'broker', 'checklist', 'stage'],
  },
  { path: '/watchlists', label: 'Watchlists', key: 'x', group: 'More', keywords: ['tickers'] },
  {
    path: '/calendar',
    label: 'Calendar',
    group: 'More',
    keywords: ['earnings', 'dividends', 'economic', 'news', 'sentiment'],
  },
  {
    path: '/screener',
    label: 'Screener',
    key: 'f',
    group: 'More',
    keywords: ['screen', 'filter', 'fundamentals'],
  },
  {
    path: '/paper',
    label: 'Trial results',
    key: 'w',
    group: 'More',
    keywords: ['on trial', 'test book', 'paper trading'],
  },
  { path: '/leaderboard', label: 'Leaderboard', key: 'b', group: 'More', keywords: ['rank'] },
  {
    path: '/trades',
    label: 'Trade costs',
    key: 't',
    group: 'More',
    keywords: ['costs', 'journal', 'slippage'],
  },
  {
    path: '/assistant',
    label: 'Assistant',
    key: 'y',
    group: 'More',
    feature: 'assistant',
    keywords: ['chat', 'ask', 'ai'],
  },

  {
    path: '/studio',
    label: 'Studio',
    key: 'u',
    group: 'Advanced',
    permission: 'lab.run',
    keywords: ['build', 'rules'],
  },
  {
    path: '/lab',
    label: 'Lab',
    key: 'l',
    group: 'Advanced',
    permission: 'lab.run',
    keywords: ['backtest', 'test'],
  },
  {
    path: '/options',
    label: 'Options',
    group: 'Advanced',
    keywords: ['chain', 'greeks', 'payoff', 'calls', 'puts', 'implied vol'],
  },
  {
    path: '/go-live',
    label: 'Strategy review',
    key: 'g',
    group: 'Advanced',
    keywords: ['checks', 'approve', 'go-live check'],
  },
  {
    // A trader's kill switches and breakers (F11): the page shows only their own.
    path: '/ops/halts',
    label: 'Halts',
    key: 'k',
    group: 'Advanced',
    permission: 'killswitch.user',
    notForAdmins: true,
    keywords: ['kill switch', 'stop trading', 'resume', 'breaker'],
  },

  {
    path: '/dashboard',
    label: 'Dashboard',
    key: 'd',
    group: 'System',
    adminOnly: true,
    keywords: ['overview', 'system'],
  },
  {
    path: '/health',
    label: 'Health',
    key: 'h',
    group: 'System',
    adminOnly: true,
    keywords: ['alerts', 'status'],
  },
  {
    path: '/live',
    label: 'Live engine',
    group: 'System',
    adminOnly: true,
    keywords: ['intraday', 'stream', 'latency', 'engine'],
  },
  {
    path: '/ops/schedule',
    label: 'Schedule',
    key: 'j',
    group: 'System',
    adminOnly: true,
    keywords: ['backups', 'jobs'],
  },
  {
    path: '/data',
    label: 'Data',
    key: 'a',
    group: 'System',
    adminOnly: true,
    keywords: ['prices', 'update data'],
  },
  { path: '/ops/data-quality', label: 'Data quality', key: 'q', group: 'System', adminOnly: true },
  {
    path: '/universes',
    label: 'Universes',
    key: 'v',
    group: 'System',
    adminOnly: true,
    keywords: ['universe', 'index'],
  },
  {
    path: '/ops/models',
    label: 'Model versions',
    group: 'System',
    adminOnly: true,
    keywords: ['retrain', 'candidates', 'swap'],
  },
  {
    path: '/ops/halts',
    label: 'Halts',
    key: 'k',
    group: 'System',
    adminOnly: true,
    keywords: ['kill switch', 'stop trading', 'resume'],
  },
  {
    path: '/admin/users',
    label: 'Users',
    key: 'r',
    group: 'System',
    adminOnly: true,
    keywords: ['people', 'roles'],
  },

  {
    path: '/profile',
    label: 'Profile',
    key: 'p',
    group: 'Account',
    keywords: ['password', 'portfolios', 'security'],
  },
  { path: '/settings', label: 'Settings', key: ',', group: 'Account', keywords: ['preferences'] },
  {
    path: '/connections',
    label: 'Broker connections',
    key: 'c',
    group: 'Account',
    keywords: ['broker'],
  },
  {
    path: '/welcome',
    label: 'Get set up',
    group: 'Account',
    keywords: ['start', 'guide', 'welcome'],
  },
  {
    path: '/help/glossary',
    label: 'Help',
    key: 'i',
    group: 'Account',
    keywords: ['glossary', 'words', 'definitions'],
  },
];

/** Folding groups in the sidebar, under the main list, in order. */
export const NAV_GROUPS = ['More', 'Advanced', 'System'] as const;
export type FoldingGroup = (typeof NAV_GROUPS)[number];

/** Open the first time someone sees the rail. The group holding the page always opens. */
export const NAV_GROUPS_OPEN_BY_DEFAULT: readonly FoldingGroup[] = ['More'];

/** What the nav needs to know about the user. `SessionService` fits. */
export interface NavViewer {
  can(permission: Permission): boolean;
  isAdmin(): boolean;
  /** False when the server has the feature off. Missing: every feature counts as on. */
  hasFeature?(feature: Feature): boolean;
}

/**
 * The viewer the nav, the palette and the shortcuts filter with. Open reads
 * (the dev profile, nobody signed in) show everything, as the server lets
 * every call through there. `features` hides items whose feature is off.
 */
export function navViewer(
  session: {
    me(): unknown;
    canRead(): boolean;
    can(permission: Permission): boolean;
    isAdmin(): boolean;
  },
  features?: { on(feature: Feature): boolean },
): NavViewer {
  const open = () => session.me() === null && session.canRead();
  return {
    can: (permission) => open() || session.can(permission),
    isAdmin: () => open() || session.isAdmin(),
    hasFeature: (feature) => features?.on(feature) ?? true,
  };
}

/** May this user see the item? Reads signals, so it is reactive inside `computed()`. */
export function navItemVisible(item: NavItem, viewer: NavViewer): boolean {
  if (item.adminOnly && !viewer.isAdmin()) return false;
  if (item.notForAdmins && viewer.isAdmin()) return false;
  if (item.permission && !viewer.can(item.permission)) return false;
  if (item.feature && viewer.hasFeature && !viewer.hasFeature(item.feature)) return false;
  return true;
}

/**
 * The groups whose item matches `url` best (the longest path that prefixes
 * it). A page listed twice (Halts: Advanced for traders, System for admins)
 * gives both groups; each viewer only sees one of them.
 */
export function groupsForUrl(url: string, items: readonly NavItem[] = NAV_ITEMS): NavGroup[] {
  const path = url.split(/[?#]/)[0] || '/';
  let best = -1;
  let groups: NavGroup[] = [];
  for (const item of items) {
    const hit =
      item.path === '/' ? path === '/' : path === item.path || path.startsWith(item.path + '/');
    if (!hit) continue;
    if (item.path.length > best) {
      best = item.path.length;
      groups = [item.group];
    } else if (item.path.length === best && !groups.includes(item.group)) {
      groups.push(item.group);
    }
  }
  return groups;
}

/** The first group whose item matches `url` best, or null. */
export function groupForUrl(url: string, items: readonly NavItem[] = NAV_ITEMS): NavGroup | null {
  return groupsForUrl(url, items)[0] ?? null;
}
