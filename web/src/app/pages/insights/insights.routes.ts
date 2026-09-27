import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Insights',
    loadComponent: () => import('./insights.page').then((m) => m.InsightsPage),
  },
  {
    path: 'risk',
    title: 'Risk',
    loadComponent: () => import('./risk.page').then((m) => m.RiskPage),
  },
] satisfies Routes;
