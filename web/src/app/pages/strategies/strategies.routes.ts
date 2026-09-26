import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Strategies',
    loadComponent: () => import('./strategies.page').then((m) => m.StrategiesPage),
  },
  {
    path: ':id',
    title: 'Strategy',
    loadComponent: () => import('./strategy-detail.page').then((m) => m.StrategyDetailPage),
  },
] satisfies Routes;
