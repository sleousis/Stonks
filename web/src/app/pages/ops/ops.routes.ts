import type { Routes } from '@angular/router';

import { adminGuard } from '../../core/auth/auth.guards';

export default [
  { path: '', pathMatch: 'full', redirectTo: 'halts' },
  {
    path: 'halts',
    title: 'Halts',
    loadComponent: () => import('./halts.page').then((m) => m.HaltsPage),
  },
  {
    path: 'schedule',
    canActivate: [adminGuard],
    title: 'Schedule and backups',
    loadComponent: () => import('./schedule.page').then((m) => m.SchedulePage),
  },
  {
    path: 'data-quality',
    canActivate: [adminGuard],
    title: 'Data quality',
    loadComponent: () => import('./data-quality.page').then((m) => m.DataQualityPage),
  },
  {
    path: 'models',
    canActivate: [adminGuard],
    title: 'Model versions',
    loadComponent: () => import('./models.page').then((m) => m.ModelsPage),
  },
] satisfies Routes;
