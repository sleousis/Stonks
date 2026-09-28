import { inject } from '@angular/core';
import { type CanActivateFn, RedirectCommand, Router, type Routes } from '@angular/router';

import { SessionService } from './session.service';

/**
 * A return path from `?next=`, kept only when it stays inside the app
 * (no other origin, no protocol-relative `//host`, not the login page).
 */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) {
    return '/';
  }
  if (next === '/login' || next.startsWith('/login?') || next.startsWith('/login/')) return '/';
  return next;
}

/**
 * Every app page: signed in (session or API token), or reads are open (dev
 * profile, the console then works read-only as before). Otherwise sign in,
 * and come back here afterwards.
 */
export const authGuard: CanActivateFn = async (_route, state) => {
  const session = inject(SessionService);
  const router = inject(Router);
  const status = await session.load();
  switch (status) {
    case 'signed-in':
    case 'open':
    case 'unreachable':
      return true;
    case 'mfa-pending':
      return router.createUrlTree(['/login'], {
        queryParams: { step: 'code', next: state.url },
      });
    default:
      return router.createUrlTree(['/login'], { queryParams: { next: state.url } });
  }
};

/** Where anyone who may not open a page lands (m10): one plain "No access" page. */
export const NO_ACCESS_PATH = '/no-access';

/**
 * Admin pages (users, the dashboard). Everyone else sees the one "No
 * access" page, at the address they asked for, instead of a silent jump.
 */
export const adminGuard: CanActivateFn = async () => {
  const session = inject(SessionService);
  const router = inject(Router);
  await session.load();
  if (session.isAdmin()) return true;
  return new RedirectCommand(router.parseUrl(NO_ACCESS_PATH), { skipLocationChange: true });
};

/** The sign-in page: someone already signed in goes where they were heading. */
export const guestGuard: CanActivateFn = async (route) => {
  const session = inject(SessionService);
  const router = inject(Router);
  const status = await session.load();
  if (status !== 'signed-in') return true;
  return router.parseUrl(safeNext(route.queryParamMap.get('next')));
};

/**
 * Put `authGuard` first on every top-level route, except those marked
 * `data: { public: true }` (sign-in). Applied once in app.config.ts, so a
 * new page is protected without remembering to.
 */
export function protectRoutes(routes: Routes): Routes {
  return routes.map((route) =>
    route.data?.['public'] || route.redirectTo !== undefined
      ? route
      : { ...route, canActivate: [authGuard, ...(route.canActivate ?? [])] },
  );
}
