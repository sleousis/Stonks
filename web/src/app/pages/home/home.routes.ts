import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Home',
    loadComponent: () => import('./home.page').then((m) => m.HomePage),
  },
] satisfies Routes;
