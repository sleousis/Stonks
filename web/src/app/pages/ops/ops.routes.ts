import type { Routes } from '@angular/router';

export default [
  { path: '', pathMatch: 'full', redirectTo: 'halts' },
  {
    path: 'halts',
    title: 'Halts',
    loadComponent: () => import('./halts.page').then((m) => m.HaltsPage),
  },
  {
    path: 'schedule',
    title: 'Schedule and backups',
    loadComponent: () => import('./schedule.page').then((m) => m.SchedulePage),
  },
  {
    path: 'data-quality',
    title: 'Data quality',
    loadComponent: () => import('./data-quality.page').then((m) => m.DataQualityPage),
  },
] satisfies Routes;
