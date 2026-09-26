/** Left navigation, in order. `key` is the "g then key" keyboard shortcut. */
export interface NavItem {
  path: string;
  label: string;
  key: string;
  /** Section heading the item sits under. */
  group: 'Monitor' | 'Build' | 'Operate' | 'System';
}

export const NAV_ITEMS: readonly NavItem[] = [
  { path: '/', label: 'Dashboard', key: 'd', group: 'Monitor' },
  { path: '/strategies', label: 'Strategies', key: 's', group: 'Monitor' },
  { path: '/shadow', label: 'Shadow', key: 'w', group: 'Monitor' },
  { path: '/orders', label: 'Orders', key: 'o', group: 'Monitor' },
  { path: '/studio', label: 'Studio', key: 'u', group: 'Build' },
  { path: '/lab', label: 'Lab', key: 'l', group: 'Build' },
  { path: '/data', label: 'Data', key: 'a', group: 'Operate' },
  { path: '/go-live', label: 'Go-live', key: 'g', group: 'Operate' },
  // ops
  { path: '/ops/halts', label: 'Halts', key: 'k', group: 'Operate' },
  { path: '/health', label: 'Health', key: 'h', group: 'System' },
  { path: '/settings', label: 'Settings', key: ',', group: 'System' },
];

export const NAV_GROUPS = ['Monitor', 'Build', 'Operate', 'System'] as const;
