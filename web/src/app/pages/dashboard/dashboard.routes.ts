import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Dashboard',
    loadComponent: () => import('./dashboard.page').then((m) => m.DashboardPage),
  },
] satisfies Routes;
