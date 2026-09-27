import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Today',
    loadComponent: () => import('./home.page').then((m) => m.HomePage),
  },
] satisfies Routes;
