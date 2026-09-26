import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Strategies',
    loadComponent: () => import('./strategies.page').then((m) => m.StrategiesPage),
  },
] satisfies Routes;
