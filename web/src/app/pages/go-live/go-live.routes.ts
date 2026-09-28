import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Strategy review',
    loadComponent: () => import('./go-live.page').then((m) => m.GoLivePage),
  },
] satisfies Routes;
