import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Going live',
    loadComponent: () => import('./going-live.page').then((m) => m.GoingLivePage),
  },
] satisfies Routes;
