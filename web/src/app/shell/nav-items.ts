import type { Permission } from '../core/auth/permissions';

/**
 * Where an item sits:
 * - `Main`: the trader's pages, on top for everyone signed in;
 * - `Research`: paper trading, the leaderboard and the build tools;
 * - `System`: the operator pages, admins only;
 * - `Account`: the menu by the user's name in the sidebar footer and drawer.
 */
export type NavGroup = 'Main' | 'Research' | 'System' | 'Account';

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
  /** Extra words that find it in the command palette. */
  keywords?: readonly string[];
}

/**
 * The navigation, in order (UX-10). No fold: a trader sees Strategies and
 * Orders at once, and the operator pages sit in an admin-only System group.
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
  { path: '/charts', label: 'Charts', key: 'z', group: 'Main', keywords: ['price'] },
  { path: '/watchlists', label: 'Watchlists', key: 'x', group: 'Main', keywords: ['tickers'] },
  {
    path: '/calendar',
    label: 'Calendar',
    group: 'Main',
    keywords: ['earnings', 'dividends', 'economic', 'news', 'sentiment'],
  },
  {
    path: '/insights',
    label: 'Insights',
    key: 'e',
    group: 'Main',
    keywords: ['portfolio', 'risk'],
  },
  { path: '/notifications', label: 'Notifications', key: 'n', group: 'Main', keywords: ['alerts'] },

  { path: '/paper', label: 'Paper trading', key: 'w', group: 'Research', keywords: ['shadow'] },
  { path: '/leaderboard', label: 'Leaderboard', key: 'b', group: 'Research', keywords: ['rank'] },
  {
    path: '/screener',
    label: 'Screener',
    key: 'f',
    group: 'Research',
    keywords: ['screen', 'filter', 'fundamentals', 'universe'],
  },
  {
    path: '/studio',
    label: 'Studio',
    key: 'u',
    group: 'Research',
    permission: 'lab.run',
    keywords: ['build', 'rules'],
  },
  {
    path: '/lab',
    label: 'Lab',
    key: 'l',
    group: 'Research',
    permission: 'lab.run',
    keywords: ['backtest', 'test'],
  },
  { path: '/go-live', label: 'Go live', key: 'g', group: 'Research', keywords: ['checks'] },
  {
    path: '/assistant',
    label: 'Assistant',
    key: 'y',
    group: 'Research',
    keywords: ['chat', 'ask', 'ai'],
  },

  {
    path: '/dashboard',
    label: 'Overview',
    key: 'd',
    group: 'System',
    adminOnly: true,
    keywords: ['dashboard'],
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
  { path: '/universes', label: 'Universes', key: 'v', group: 'System', adminOnly: true },
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
    keywords: ['password', 'portfolios'],
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
    label: 'Glossary',
    key: 'i',
    group: 'Account',
    keywords: ['help', 'words'],
  },
];

/** Headed groups in the sidebar, under the main list. */
export const NAV_GROUPS = ['Research', 'System'] as const;

/** What the nav needs to know about the user. `SessionService` fits. */
export interface NavViewer {
  can(permission: Permission): boolean;
  isAdmin(): boolean;
}

/**
 * The viewer the nav, the palette and the shortcuts filter with. Open reads
 * (the dev profile, nobody signed in) show everything, as the server lets
 * every call through there.
 */
export function navViewer(session: {
  me(): unknown;
  canRead(): boolean;
  can(permission: Permission): boolean;
  isAdmin(): boolean;
}): NavViewer {
  const open = () => session.me() === null && session.canRead();
  return {
    can: (permission) => open() || session.can(permission),
    isAdmin: () => open() || session.isAdmin(),
  };
}

/** May this user see the item? Reads signals, so it is reactive inside `computed()`. */
export function navItemVisible(item: NavItem, viewer: NavViewer): boolean {
  if (item.adminOnly && !viewer.isAdmin()) return false;
  if (item.permission && !viewer.can(item.permission)) return false;
  return true;
}
