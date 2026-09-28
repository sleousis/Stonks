import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Trial results',
    loadComponent: () => import('./shadow.page').then((m) => m.ShadowPage),
  },
] satisfies Routes;
