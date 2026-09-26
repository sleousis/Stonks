import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Trade costs',
    loadComponent: () => import('./trades.page').then((m) => m.TradesPage),
  },
] satisfies Routes;
