import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Shadow',
    loadComponent: () => import('./shadow.page').then((m) => m.ShadowPage),
  },
] satisfies Routes;
