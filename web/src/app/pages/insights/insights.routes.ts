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
  {
    path: 'cash-flows',
    title: 'Cash flows',
    loadComponent: () => import('./cash-flows.page').then((m) => m.CashFlowsPage),
  },
  {
    path: 'tax',
    title: 'Tax',
    loadComponent: () => import('./tax.page').then((m) => m.TaxPage),
  },
] satisfies Routes;
