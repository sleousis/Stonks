import type { Permission } from '../core/auth/permissions';
import type { Feature } from '../core/features/feature-flags.service';

/**
 * The workspaces across the top of the console. Each holds a handful of
 * pages, shown as tabs in the chrome bar:
 * - `Trade`: your day, orders and positions;
 * - `Research`: find, build and test strategies;
 * - `Portfolio`: your money, risk and the road to live;
 * - `System`: the operator pages, admins only;
 * - `Account`: the menu behind the avatar.
 */
export type Workspace = 'Trade' | 'Research' | 'Portfolio' | 'System' | 'Account';

/** The workspaces with tabs, in the switcher's order. */
export type TabWorkspace = Exclude<Workspace, 'Account'>;

/** The workspaces in the switcher, in order, with a one-line blurb. */
export const WORKSPACES: readonly { name: TabWorkspace; blurb: string }[] = [
  { name: 'Trade', blurb: 'Your day, orders and positions' },
  { name: 'Research', blurb: 'Find, build and test strategies' },
  { name: 'Portfolio', blurb: 'Your money, risk and the road to live' },
  { name: 'System', blurb: 'Health, schedule, data and people' },
];

/** One page in the navigation. `key` is the "g then key" keyboard shortcut. */
export interface NavItem {
  path: string;
  label: string;
  key?: string;
  workspace: Workspace;
  /** Shown only to users with this permission (the page is for doing, not reading). */
  permission?: Permission;
  /** Shown to admins only (the route or the API still checks). */
  adminOnly?: boolean;
  /** Hidden from admins, who reach the same page from the System workspace. */
  notForAdmins?: boolean;
  /** Hidden while the server has this feature off (F42). */
  feature?: Feature;
  /** Extra words that find it in the command palette. */
  keywords?: readonly string[];
}

/**
 * The navigation, by workspace and in tab order. Every page keeps its path,
 * shortcut and words. Words follow docs/design/vocabulary.md.
 */
export const NAV_ITEMS: readonly NavItem[] = [
  // Trade: your day, orders and positions.
  { path: '/', label: 'Today', key: 'm', workspace: 'Trade', keywords: ['home'] },
  { path: '/orders', label: 'Orders', key: 'o', workspace: 'Trade', keywords: ['trades', 'fills'] },
  {
    // The inbox only fills for Approve each trade on a broker portfolio.
    path: '/tickets',
    label: 'Approvals',
    workspace: 'Trade',
    permission: 'portfolio.trade',
    keywords: ['tickets', 'approve', 'order tickets'],
  },
  { path: '/charts', label: 'Charts', key: 'z', workspace: 'Trade', keywords: ['price'] },
  { path: '/watchlists', label: 'Watchlists', key: 'x', workspace: 'Trade', keywords: ['tickers'] },
  {
    path: '/calendar',
    label: 'Calendar',
    workspace: 'Trade',
    keywords: ['earnings', 'dividends', 'economic', 'news', 'sentiment'],
  },
  {
    path: '/trades',
    label: 'Trade costs',
    key: 't',
    workspace: 'Trade',
    keywords: ['costs', 'slippage'],
  },
  {
    path: '/journal',
    label: 'Journal',
    workspace: 'Trade',
    keywords: ['round trips', 'calendar', 'playbooks', 'review'],
  },

  // Research: find, build and test strategies.
  {
    path: '/strategies',
    label: 'Strategies',
    key: 's',
    workspace: 'Research',
    keywords: ['follow'],
  },
  {
    path: '/studio',
    label: 'Build a strategy',
    key: 'u',
    workspace: 'Research',
    permission: 'lab.run',
    keywords: ['studio', 'build', 'rules', 'new strategy'],
  },
  {
    path: '/lab',
    label: 'Lab',
    key: 'l',
    workspace: 'Research',
    permission: 'lab.run',
    keywords: ['backtest', 'test'],
  },
  {
    path: '/paper',
    label: 'Trial results',
    key: 'w',
    workspace: 'Research',
    keywords: ['on trial', 'test book', 'paper trading'],
  },
  {
    path: '/go-live',
    label: 'Strategy review',
    key: 'g',
    workspace: 'Research',
    // The approval queue: approving is for admins (route guarded too).
    adminOnly: true,
    keywords: ['checks', 'approve', 'go-live check'],
  },
  {
    path: '/leaderboard',
    label: 'Leaderboard',
    key: 'b',
    workspace: 'Research',
    keywords: ['rank'],
  },
  {
    path: '/screener',
    label: 'Screener',
    key: 'f',
    workspace: 'Research',
    keywords: ['screen', 'filter', 'fundamentals'],
  },
  {
    path: '/options',
    label: 'Options',
    workspace: 'Research',
    feature: 'options',
    keywords: ['chain', 'greeks', 'payoff', 'calls', 'puts', 'implied vol'],
  },
  {
    path: '/assistant',
    label: 'Assistant',
    key: 'y',
    workspace: 'Research',
    feature: 'assistant',
    keywords: ['chat', 'ask', 'ai'],
  },

  // Portfolio: your money, risk and the road to live.
  {
    path: '/insights',
    label: 'Insights',
    key: 'e',
    workspace: 'Portfolio',
    keywords: ['portfolio', 'risk', 'value'],
  },
  {
    path: '/going-live',
    label: 'Going live',
    workspace: 'Portfolio',
    keywords: ['real money', 'broker', 'checklist', 'stage'],
  },
  {
    // A trader's kill switches and breakers (F11): the page shows only their own.
    path: '/ops/halts',
    label: 'Halts',
    key: 'k',
    workspace: 'Portfolio',
    permission: 'killswitch.user',
    notForAdmins: true,
    keywords: ['kill switch', 'stop trading', 'resume', 'breaker'],
  },
  {
    path: '/connections',
    label: 'Broker connections',
    key: 'c',
    workspace: 'Portfolio',
    keywords: ['broker'],
  },

  // System: the operator pages, admins only.
  {
    path: '/dashboard',
    label: 'Dashboard',
    key: 'd',
    workspace: 'System',
    adminOnly: true,
    keywords: ['overview', 'system'],
  },
  {
    path: '/health',
    label: 'Health',
    key: 'h',
    workspace: 'System',
    adminOnly: true,
    keywords: ['alerts', 'status'],
  },
  {
    path: '/live',
    label: 'Intraday engine',
    workspace: 'System',
    adminOnly: true,
    feature: 'intraday',
    keywords: ['intraday', 'stream', 'latency', 'engine'],
  },
  {
    path: '/ops/schedule',
    label: 'Schedule',
    key: 'j',
    workspace: 'System',
    adminOnly: true,
    keywords: ['backups', 'jobs'],
  },
  {
    path: '/data',
    label: 'Data',
    key: 'a',
    workspace: 'System',
    adminOnly: true,
    keywords: ['prices', 'update data'],
  },
  {
    path: '/ops/data-quality',
    label: 'Data quality',
    key: 'q',
    workspace: 'System',
    adminOnly: true,
  },
  {
    path: '/universes',
    label: 'Universes',
    key: 'v',
    workspace: 'System',
    adminOnly: true,
    keywords: ['universe', 'index'],
  },
  {
    path: '/ops/models',
    label: 'Model versions',
    workspace: 'System',
    adminOnly: true,
    feature: 'models',
    keywords: ['retrain', 'candidates', 'swap'],
  },
  {
    path: '/ops/halts',
    label: 'Halts',
    key: 'k',
    workspace: 'System',
    adminOnly: true,
    keywords: ['kill switch', 'stop trading', 'resume'],
  },
  {
    path: '/admin/users',
    label: 'Users',
    key: 'r',
    workspace: 'System',
    adminOnly: true,
    keywords: ['people', 'roles'],
  },

  // Account: the avatar menu.
  {
    path: '/profile',
    label: 'Profile',
    key: 'p',
    workspace: 'Account',
    keywords: ['password', 'portfolios', 'security'],
  },
  {
    path: '/settings',
    label: 'Settings',
    key: ',',
    workspace: 'Account',
    keywords: ['preferences'],
  },
  {
    path: '/welcome',
    label: 'Get set up',
    workspace: 'Account',
    keywords: ['start', 'guide', 'welcome'],
  },
  {
    path: '/help/glossary',
    label: 'Help',
    key: 'i',
    workspace: 'Account',
    keywords: ['glossary', 'words', 'definitions'],
  },
  {
    // The bell opens the feed too; the menu, the palette and g n find it.
    path: '/notifications',
    label: 'Notifications',
    key: 'n',
    workspace: 'Account',
    keywords: ['alerts', 'feed', 'bell'],
  },
];

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

/** Does `item` hold `path` (the page itself or a page below it)? */
function holds(item: NavItem, path: string): boolean {
  return item.path === '/' ? path === '/' : path === item.path || path.startsWith(item.path + '/');
}

/** The item that matches `url` best (the longest path that prefixes it) among `items`. */
export function itemForUrl(url: string, items: readonly NavItem[] = NAV_ITEMS): NavItem | null {
  const path = url.split(/[?#]/)[0] || '/';
  let best: NavItem | null = null;
  for (const item of items) {
    if (holds(item, path) && (!best || item.path.length > best.path.length)) best = item;
  }
  return best;
}

/**
 * The workspaces whose item matches `url` best (the longest path that
 * prefixes it). A page listed twice (Halts: Portfolio for traders, System
 * for admins) gives both; each viewer only sees one of them.
 */
export function workspacesForUrl(url: string, items: readonly NavItem[] = NAV_ITEMS): Workspace[] {
  const path = url.split(/[?#]/)[0] || '/';
  let best = -1;
  let found: Workspace[] = [];
  for (const item of items) {
    if (!holds(item, path)) continue;
    if (item.path.length > best) {
      best = item.path.length;
      found = [item.workspace];
    } else if (item.path.length === best && !found.includes(item.workspace)) {
      found.push(item.workspace);
    }
  }
  return found;
}

/** The first workspace whose item matches `url` best, or null. */
export function workspaceForUrl(
  url: string,
  items: readonly NavItem[] = NAV_ITEMS,
): Workspace | null {
  return workspacesForUrl(url, items)[0] ?? null;
}
