import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Get set up',
    loadComponent: () => import('./welcome.page').then((m) => m.WelcomePage),
  },
] satisfies Routes;
