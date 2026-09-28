import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Screener',
    loadComponent: () => import('./screener.page').then((m) => m.ScreenerPage),
  },
] satisfies Routes;
