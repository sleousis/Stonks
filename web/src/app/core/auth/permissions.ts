import type { ApiScope, MeView, Role } from '../../api/models';
import { ROUTE_PERMISSIONS, type RoutePermission } from './route-permissions.gen';

/**
 * Who may do what, mirrored from the server's policy (`stonks.auth.policy`)
 * so the console can hide or disable actions the user lacks. The server
 * still decides: this only saves a trader from filling in a form that ends
 * in a 403. Routes name their permission in openapi.json (`x-permission`),
 * collected in `route-permissions.gen.ts`.
 *
 * Step-up (a fresh second factor) is not checked here: the session
 * interceptor asks for a code when the API wants one.
 */
export type Permission = RoutePermission | 'subscription.auto_enable' | 'killswitch.global';

interface Rule {
  roles: readonly Role[];
  /** Any one of these credential scopes grants it. */
  scopes: readonly ApiScope[];
  /** Only a signed-in browser session, never an API token. */
  sessionOnly?: boolean;
}

const ALL: readonly Role[] = ['viewer', 'trader', 'admin'];
const TRADERS: readonly Role[] = ['trader', 'admin'];
const ADMINS: readonly Role[] = ['admin'];
const ALL_SCOPES: readonly ApiScope[] = ['read', 'trade', 'lab', 'admin'];

export const POLICY: Readonly<Record<Permission, Rule>> = {
  'data.read': { roles: ALL, scopes: ['read'] },
  'portfolio.manage': { roles: TRADERS, scopes: ['trade'] },
  'subscription.auto_enable': { roles: TRADERS, scopes: ['trade'] },
  'connection.manage': { roles: TRADERS, scopes: ['trade'] },
  'lab.run': { roles: TRADERS, scopes: ['lab'] },
  'killswitch.user': { roles: TRADERS, scopes: ['trade'] },
  'killswitch.resume': { roles: TRADERS, scopes: ['trade'] },
  'risk.reset': { roles: TRADERS, scopes: ['trade'] },
  'notifications.manage': { roles: TRADERS, scopes: ['trade'] },
  'killswitch.global': { roles: ADMINS, scopes: ['admin'] },
  'strategy.promote': { roles: ADMINS, scopes: ['admin'] },
  'operations.run': { roles: ADMINS, scopes: ['admin'] },
  'risk.global': { roles: ADMINS, scopes: ['admin'] },
  'portfolio.totals': { roles: ADMINS, scopes: ['admin'] },
  'users.read': { roles: ADMINS, scopes: ['admin'] },
  'users.manage': { roles: ADMINS, scopes: ['admin'] },
  'tokens.manage': { roles: ALL, scopes: ['read'], sessionOnly: true },
  'tokens.revoke': { roles: ALL, scopes: ALL_SCOPES },
  'mfa.recovery_codes': { roles: ALL, scopes: ['read'] },
  'password.change': { roles: ALL, scopes: ['read'] },
};

/** True when `me` holds `permission` (false when nobody is signed in). */
export function allowed(me: MeView | null, permission: Permission): boolean {
  return denial(me, permission) === null;
}

/**
 * Why `me` may not do this, in a few plain words for a hint under a disabled
 * button, or null when allowed.
 */
export function denial(me: MeView | null, permission: Permission): string | null {
  const rule = POLICY[permission];
  if (!me) return 'Sign in to do this.';
  if (!rule.roles.includes(me.role)) {
    return rule.roles.includes('trader') ? 'Traders and admins only.' : 'Admins only.';
  }
  if (!me.scopes.some((s) => rule.scopes.includes(s))) {
    return 'Your API token does not allow this. Sign in instead.';
  }
  if (rule.sessionOnly && me.via !== 'session') return 'Sign in with your password to do this.';
  return null;
}

/**
 * The permission a route needs, from openapi.json. `path` is the template
 * (`/api/strategies/{strategy_id}/promote`); null for routes open to anyone
 * signed in.
 */
export function routePermission(method: string, path: string): RoutePermission | null {
  return ROUTE_PERMISSIONS[`${method.toUpperCase()} ${path}`] ?? null;
}
