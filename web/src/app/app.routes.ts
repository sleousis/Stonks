import type { Routes } from '@angular/router';

import { adminGuard } from './core/auth/auth.guards';

/**
 * One lazy route file per page (`pages/<name>/<name>.routes.ts`). Page agents
 * add child routes inside their own route file, not here.
 *
 * Every route needs a signed-in user (or open reads in dev): app.config.ts
 * wraps this list with `protectRoutes()`. Mark a page `data: { public: true }`
 * to skip that, and `bare: true` to show it without the app frame.
 */
export const routes: Routes = [
  // Sign-in and the trader's own pages (S9) --------------------------------
  {
    path: 'login',
    data: { public: true, bare: true },
    loadChildren: () => import('./pages/login/login.routes'),
  },
  { path: '', pathMatch: 'full', loadChildren: () => import('./pages/home/home.routes') },
  { path: 'profile', loadChildren: () => import('./pages/profile/profile.routes') },
  {
    path: 'admin/users',
    canActivate: [adminGuard],
    loadChildren: () => import('./pages/admin-users/admin-users.routes'),
  },
  // ---------------------------------------------------------------------------
  { path: 'dashboard', loadChildren: () => import('./pages/dashboard/dashboard.routes') },
  { path: 'strategies', loadChildren: () => import('./pages/strategies/strategies.routes') },
  { path: 'studio', loadChildren: () => import('./pages/studio/studio.routes') },
  { path: 'lab', loadChildren: () => import('./pages/lab/lab.routes') },
  { path: 'data', loadChildren: () => import('./pages/data/data.routes') },
  { path: 'orders', loadChildren: () => import('./pages/orders/orders.routes') },
  { path: 'shadow', loadChildren: () => import('./pages/shadow/shadow.routes') },
  { path: 'go-live', loadChildren: () => import('./pages/go-live/go-live.routes') },
  { path: 'health', loadChildren: () => import('./pages/health/health.routes') },
  { path: 'settings', loadChildren: () => import('./pages/settings/settings.routes') },
  // ops: halts, schedule and backups, data quality, universes
  { path: 'ops', loadChildren: () => import('./pages/ops/ops.routes') },
  { path: 'universes', loadChildren: () => import('./pages/universes/universes.routes') },
  { path: 'help', loadChildren: () => import('./pages/help/help.routes') },
  {
    path: '**',
    title: 'Not found',
    loadComponent: () => import('./pages/not-found.page').then((m) => m.NotFoundPage),
  },
];
