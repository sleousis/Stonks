import type { Route } from '@angular/router';

import { routes } from './app.routes';
import { adminGuard } from './core/auth/auth.guards';
import { oldReviewLinkGuard } from './pages/go-live/go-live.guards';
import goLiveRoutes from './pages/go-live/go-live.routes';
import opsRoutes from './pages/ops/ops.routes';
import { NAV_ITEMS } from './shell/nav-items';

/**
 * Admin nav items that traders still reach from their own pages: the Lab,
 * the screener and charts link to universes and data. Their APIs check.
 */
const OPEN_TO_TRADERS = new Set(['/data', '/universes', '/ops/halts']);

function guarded(route: Route | undefined): boolean {
  return !!route?.canActivate?.includes(adminGuard);
}

/** The route for a nav path, top level or one ops child, and whether a guard covers it. */
function adminGuarded(path: string): boolean {
  const bare = path.replace(/^\//, '');
  const [first, child] = bare.split('/');
  if (guarded(routes.find((r) => r.path === bare))) return true;
  if (guarded(routes.find((r) => r.path === first))) return true;
  if (first === 'ops' && child) return guarded(opsRoutes.find((r) => r.path === child));
  if (first === 'go-live') return guarded(goLiveRoutes.find((r) => r.path === ''));
  return false;
}

describe('app routes', () => {
  it('guards every admin-only nav page', () => {
    const open = NAV_ITEMS.filter((i) => i.adminOnly && !OPEN_TO_TRADERS.has(i.path))
      .map((i) => i.path)
      .filter((p) => !adminGuarded(p));
    expect(open).toEqual([]);
  });

  it('guards Strategy review for admins, after the old ?strategy= link redirect', () => {
    const route = goLiveRoutes.find((r) => r.path === '');
    expect(route?.canActivate).toEqual([oldReviewLinkGuard, adminGuard]);
    expect(adminGuarded('/go-live')).toBe(true);
    expect(NAV_ITEMS.find((i) => i.path === '/go-live')?.adminOnly).toBe(true);
  });

  it('leaves the halts page open for a trader', () => {
    expect(adminGuarded('/ops/halts')).toBe(false);
  });

  it('shows every admin-guarded page only to admins in the nav', () => {
    const shownToAll = NAV_ITEMS.filter((i) => !i.adminOnly)
      .map((i) => i.path)
      .filter((p) => adminGuarded(p));
    expect(shownToAll).toEqual([]);
  });
});
