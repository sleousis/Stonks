/** Left navigation, in order. `key` is the "g then key" keyboard shortcut. */
export interface NavItem {
  path: string;
  label: string;
  key: string;
  /** Section heading the item sits under. */
  group: 'You' | 'Monitor' | 'Build' | 'Operate' | 'System';
  /** Shown to admins only (the route has its own guard too). */
  adminOnly?: boolean;
}

export const NAV_ITEMS: readonly NavItem[] = [
  // The trader's own pages (S9): always on top, never folded away.
  { path: '/', label: 'Today', key: 'm', group: 'You' },
  { path: '/insights', label: 'Insights', key: 'e', group: 'You' },
  { path: '/notifications', label: 'Notifications', key: 'n', group: 'You' },
  { path: '/connections', label: 'Broker connections', key: 'c', group: 'You' },
  { path: '/profile', label: 'Profile', key: 'p', group: 'You' },
  { path: '/admin/users', label: 'Users', key: 'r', group: 'You', adminOnly: true },
  // Advanced pages.
  { path: '/dashboard', label: 'Dashboard', key: 'd', group: 'Monitor' },
  { path: '/strategies', label: 'Strategies', key: 's', group: 'Monitor' },
  { path: '/shadow', label: 'Shadow', key: 'w', group: 'Monitor' },
  { path: '/orders', label: 'Orders', key: 'o', group: 'Monitor' },
  { path: '/trades', label: 'Trade costs', key: 't', group: 'Monitor' },
  { path: '/studio', label: 'Studio', key: 'u', group: 'Build' },
  { path: '/lab', label: 'Lab', key: 'l', group: 'Build' },
  { path: '/data', label: 'Data', key: 'a', group: 'Operate' },
  { path: '/go-live', label: 'Go-live', key: 'g', group: 'Operate' },
  // ops
  { path: '/universes', label: 'Universes', key: 'v', group: 'Operate' },
  { path: '/ops/halts', label: 'Halts', key: 'k', group: 'Operate' },
  { path: '/ops/schedule', label: 'Schedule', key: 'j', group: 'System' },
  { path: '/ops/data-quality', label: 'Data quality', key: 'q', group: 'System' },
  { path: '/health', label: 'Health', key: 'h', group: 'System' },
  { path: '/settings', label: 'Settings', key: ',', group: 'System' },
  { path: '/help/glossary', label: 'Glossary', key: 'i', group: 'System' },
];

/** Groups under the "Advanced" fold. 'You' sits above it. */
export const NAV_GROUPS = ['Monitor', 'Build', 'Operate', 'System'] as const;
