import type { Routes } from '@angular/router';

/**
 * One lazy route file per page (`pages/<name>/<name>.routes.ts`). Page agents
 * add child routes inside their own route file, not here.
 */
export const routes: Routes = [
  { path: '', loadChildren: () => import('./pages/dashboard/dashboard.routes') },
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
  {
    path: '**',
    title: 'Not found',
    loadComponent: () => import('./pages/not-found.page').then((m) => m.NotFoundPage),
  },
];
