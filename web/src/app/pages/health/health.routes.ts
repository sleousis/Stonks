import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Health',
    loadComponent: () => import('./health.page').then((m) => m.HealthPage),
  },
] satisfies Routes;
