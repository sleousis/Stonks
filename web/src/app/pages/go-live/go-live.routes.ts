import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Go-live',
    loadComponent: () => import('./go-live.page').then((m) => m.GoLivePage),
  },
] satisfies Routes;
