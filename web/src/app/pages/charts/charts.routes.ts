import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Charts',
    loadComponent: () => import('./chart.page').then((m) => m.ChartPage),
  },
  {
    path: ':ticker',
    title: 'Chart',
    loadComponent: () => import('./chart.page').then((m) => m.ChartPage),
  },
] satisfies Routes;
